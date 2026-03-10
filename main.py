"""
MAIN - Main Trading Bot
Orchestrator of the XAU/USD automated trading system
"""

import os
import sys
import time
import signal
import json
import subprocess
from datetime import datetime, timedelta, timezone
from typing import Optional
import traceback

# Add directory to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config
from logger import log
from state_writer import write_state, add_closed_trade
from db_writer import init_db, record_analysis, record_trade_open, record_trade_close, record_agent_decision, get_recent_agent_decisions, get_trade_feedback
from alerts import (
    alert_bot_started, alert_bot_stopped, alert_signal_detected,
    alert_safety_block, alert_error, alert_daily_summary, discord,
    alert_heartbeat_full,
    alert_market_closed, alert_market_open,
    alert_m5_reversal_block, alert_trade_resolved,
    alert_brain_decision,
    alert_spread_delay, alert_spread_skip,
    alert_agent_decision,
    check_ea_bridge_status_and_alert
)
from confluence import analyze_confluence
from confluence import is_actionable_signal as confluence_is_actionable
from confluence import get_trade_direction as confluence_get_direction
from risk_manager import calculate_position_size, calculate_sl_tp
from safety_checks import is_safe_to_trade, record_trade_result, record_trade_opened, record_close_type, get_safety_status, is_market_open
from executor import (
    connect_mt5, disconnect_mt5, is_mt5_connected,
    get_account_balance, execute_buy, execute_sell, get_positions, executor,
    get_recent_closed_deals, get_deal_history
)
from monitor import monitor_positions, get_positions_summary, close_all_positions
from technical_analyzer import get_mt5_data, calculate_indicators, calculate_technical_score, get_atr_value


# ============================================================================
# NEWS CACHE (avoid excessive requests)
# ============================================================================

class NewsCache:
    """Cache for news score"""
    
    def __init__(self, cache_minutes: int = 30):
        self.cache_minutes = cache_minutes
        self.last_fetch = None
        self.cached_score = 50.0
        self.cached_data = {}
    
    def get_score(self) -> tuple:
        """Return news score (from cache or updated)"""
        now = datetime.now()
        
        # Check if cache is still valid
        if self.last_fetch and (now - self.last_fetch) < timedelta(minutes=self.cache_minutes):
            return self.cached_score, self.cached_data
        
        # Update cache
        try:
            from news_sentiment import get_hybrid_score
            result = get_hybrid_score()
            
            self.cached_score = result.get('score', 50.0)
            self.cached_data = result
            self.last_fetch = now
            
            log.info(f"News score updated: {self.cached_score:.1f}")
            
        except Exception as e:
            log.warning(f"Error getting news score: {e}")
            # Keep previous cache
        
        return self.cached_score, self.cached_data


news_cache = NewsCache(cache_minutes=config.NEWS_CACHE_MINUTES)


# ============================================================================
# TRADING BOT
# ============================================================================

class TradingBot:
    """Main trading bot"""
    
    def __init__(self):
        self.running = False
        self.mode = config.TRADING_MODE  # "DRY_RUN", "DEMO", "LIVE"
        self.dry_run = (self.mode == "DRY_RUN")
        self.is_live = (self.mode == "LIVE")
        self.executes_trades = (self.mode in ("DEMO", "LIVE"))
        self.last_analysis = None
        self.last_known_price = None
        self.session_start_time = None
        self.session_analyses = 0
        self.daily_stats = {
            'trades': 0,
            'wins': 0,
            'losses': 0,
            'breakevens': 0,
            'pnl': 0.0,
            'date': datetime.now().date()
        }

        # Closed trades today (for dashboard)
        self.closed_trades_today = []
        
        # Heartbeat tracking
        self.last_heartbeat = None
        self.last_heartbeat_scenario = None
        self.last_heartbeat_score = None
        
        # Temporary data from last analysis (for heartbeat)
        self._last_calendar_data = None
        self._last_vol_status = None
        self._last_current_price = None
        self._last_scenario_description = None
        self._last_gpt_validation = None
        
        # Cycle Memory (cycle memory for temporal context)
        from cycle_memory import CycleMemory
        self.cycle_memory = CycleMemory()
        
        # Market state tracking (for open/close detection)
        self.market_was_open = True  # Assume open at startup
        self._last_keepalive_log = None  # Timestamp of last keepalive log (market closed)
        
        # GPT Confidence Validator stats
        self.gpt_stats = {"confirm": 0, "boost": 0, "reduce": 0, "from_cache": 0}
        
        # Configure shutdown handler
        signal.signal(signal.SIGINT, self._shutdown_handler)
        signal.signal(signal.SIGTERM, self._shutdown_handler)
    
    def _shutdown_handler(self, signum, frame):
        """Graceful shutdown handler"""
        log.info("Shutdown signal received...")
        self.running = False

    def _load_persisted_state(self) -> None:
        try:
            state_path = os.path.abspath(getattr(config, "DASHBOARD_STATE_FILE", "data/bot_state.json"))
            if not os.path.exists(state_path):
                return

            with open(state_path, "r", encoding="utf-8") as f:
                payload = json.load(f)

            last_analysis = payload.get("last_analysis")
            if isinstance(last_analysis, dict) and last_analysis:
                has_real_data = any(
                    last_analysis.get(k) is not None
                    for k in ("decision", "final_score", "current_price")
                )
                if has_real_data:
                    self.last_analysis = last_analysis

            daily_stats = payload.get("daily_stats")
            if isinstance(daily_stats, dict) and daily_stats:
                if isinstance(daily_stats.get("date"), str):
                    try:
                        daily_stats["date"] = datetime.strptime(daily_stats["date"], "%Y-%m-%d").date()
                    except Exception:
                        pass
                self.daily_stats = daily_stats

            lkp = payload.get("last_known_price")
            if lkp is not None:
                self.last_known_price = float(lkp)

            trade_history = payload.get("trade_history")
            if isinstance(trade_history, list):
                self.closed_trades_today = trade_history
        except Exception as e:
            log.debug(f"Failed to load persisted dashboard state: {e}")

    def _reconcile_with_mt5(self) -> None:
        """Reconcile saved state with MT5 reality.
        
        MT5 is the source of truth. Three passes:
        
        Pass 1 — Build closed_trades_today from TODAY's MT5 deals:
          - Replaces what was in bot_state.json
          - Only deals with close_time.date() == today go to dashboard
        
        Pass 2 — Register orphan historical trades in SQLite:
          - Bot trades (comment "Bot-") from previous days not in SQLite
          - Go ONLY to SQLite (history), NOT to dashboard "today"
        
        Pass 3 — Fix trades in SQLite without close or with estimation:
          - Trades with close_price NULL or close_reason "estimado"
          - Update with real MT5 data
        """
        try:
            if not self.executes_trades:
                return
            
            account_info = executor.get_account_info()
            if not account_info:
                log.warning("Reconciliation: could not get MT5 account info")
                return
            
            mt5_balance = account_info['balance']
            today = datetime.now().date()
            
            saved_pnl = float(self.daily_stats.get('pnl', 0.0) or 0.0)
            saved_date = self.daily_stats.get('date')
            
            # If saved state is from another day, clear (daily reset will handle)
            if saved_date and saved_date != today:
                log.info(f"Reconciliation: saved state is from {saved_date}, today is {today} — daily reset will fix")
                return
            
            # Get ALL real closing deals from MT5 (last 7 days + today)
            real_deals = get_recent_closed_deals(hours=168)
            
            # Index real deals by position_id
            real_deals_by_pos = {}
            for d in real_deals:
                real_deals_by_pos[d['position_id']] = d
            
            # Separate today's deals vs historical
            today_deals = [d for d in real_deals if d['close_time'].date() == today]
            historical_deals = [d for d in real_deals if d['close_time'].date() != today]
            
            log.info(
                f"Reconciliation: {len(real_deals)} total deals | "
                f"{len(today_deals)} today | {len(historical_deals)} historical"
            )
            
            # ================================================================
            # PASS 1: Build closed_trades_today from MT5 (today)
            # MT5 is the source of truth — replaces what was in bot_state
            # ================================================================
            self.closed_trades_today = []
            
            for deal in today_deals:
                pos_id = deal['position_id']
                log.info(
                    f"  Today #{pos_id}: {deal['direction']} | "
                    f"open={deal.get('open_price', '?')} → close={deal['close_price']:.2f} | "
                    f"P&L=${deal['profit']:+.2f} | {deal['reason']} | {deal['close_time'].strftime('%H:%M')}"
                )
                # Derive close_type from reason + P&L heuristic
                # On restart, monitor state is lost — MT5 only reports "Stop Loss" or "Take Profit"
                # Heuristic fallback: profit > $1 = trailing, profit ~$0 = breakeven, profit < 0 = SL
                deal_reason = deal['reason']
                deal_profit = deal['profit']
                if deal_reason == "Take Profit":
                    deal_close_type = "tp"
                elif deal_reason == "Stop Loss":
                    if deal_profit > 1.0:
                        deal_close_type = "trailing"
                    elif deal_profit >= 0:
                        deal_close_type = "breakeven"
                    else:
                        deal_close_type = "sl"
                else:
                    deal_close_type = "sl"
                
                self.closed_trades_today.append({
                    "ticket": pos_id, "direction": deal['direction'],
                    "volume": deal['volume'],
                    "open_price": deal.get('open_price'),
                    "close_price": deal['close_price'],
                    "profit": deal_profit,
                    "reason": deal_reason,
                    "close_time": deal['close_time'].isoformat(),
                    "close_type": deal_close_type,
                    "estimated": deal.get('estimated', False),
                })
                # Ensure it's in SQLite
                record_trade_close(
                    ticket=pos_id, close_price=deal['close_price'],
                    profit=deal['profit'], close_reason=deal['reason'],
                    close_time=deal['close_time'].isoformat(),
                )
            
            # ================================================================
            # PASS 2: Record orphan historical trades in SQLite ONLY
            # (bot trades from previous days not yet in SQLite)
            # ================================================================
            try:
                import sqlite3
                db_path = os.path.abspath(getattr(config, "HISTORY_DB_PATH", "data/history.db"))
                conn = sqlite3.connect(db_path, timeout=5)
                all_sqlite_tickets = {
                    row[0] for row in conn.execute("SELECT ticket FROM trades").fetchall()
                }
                
                # Fix trades in SQLite without close, estimation, or pending
                unclosed = conn.execute(
                    "SELECT ticket, direction, open_price FROM trades "
                    "WHERE close_price IS NULL OR close_reason LIKE '%estimado%' OR close_reason LIKE '%pending%' OR profit IS NULL"
                ).fetchall()
                conn.close()
                
                # Historical orphans → SQLite only
                orphan_count = 0
                for deal in historical_deals:
                    pos_id = deal['position_id']
                    comment = deal.get('comment', '')
                    if not comment.startswith('Bot-'):
                        continue
                    if pos_id in all_sqlite_tickets:
                        continue
                    orphan_count += 1
                    log.info(
                        f"  Historical #{pos_id}: {deal['direction']} | "
                        f"open={deal.get('open_price', '?')} → close={deal['close_price']:.2f} | "
                        f"P&L=${deal['profit']:+.2f} | {deal['reason']} | "
                        f"{deal['close_time'].strftime('%m-%d %H:%M')} → SQLite"
                    )
                    from db_writer import record_trade_open
                    record_trade_open(
                        ticket=pos_id, direction=deal['direction'],
                        volume=deal['volume'],
                        open_price=deal.get('open_price') or deal['close_price'],
                        sl=0, tp=0,
                        open_time=deal['close_time'].isoformat(),
                        comment=deal.get('comment', 'recovered'),
                    )
                    record_trade_close(
                        ticket=pos_id, close_price=deal['close_price'],
                        profit=deal['profit'], close_reason=deal['reason'],
                        close_time=deal['close_time'].isoformat(),
                    )
                    all_sqlite_tickets.add(pos_id)
                
                if orphan_count:
                    log.info(f"  → {orphan_count} historical orphan trades registered in SQLite")
                
                # ============================================================
                # PASS 3: Fix trades in SQLite without correct close
                # ============================================================
                if unclosed:
                    log.info(f"  Pass 3: {len(unclosed)} trades without correct close in SQLite")
                    for ticket, direction, open_price in unclosed:
                        deal = real_deals_by_pos.get(ticket)
                        if deal:
                            log.info(
                                f"    Resolved #{ticket}: close={deal['close_price']:.2f} | "
                                f"P&L=${deal['profit']:+.2f} | {deal['reason']}"
                            )
                            record_trade_close(
                                ticket=ticket, close_price=deal['close_price'],
                                profit=deal['profit'], close_reason=deal['reason'],
                                close_time=deal['close_time'].isoformat(),
                            )
                            # Update closed_trades_today if this trade is there as pending
                            for t in self.closed_trades_today:
                                if t.get('ticket') == ticket and t.get('pending'):
                                    t['profit'] = deal['profit']
                                    t['close_price'] = deal['close_price']
                                    t['reason'] = deal['reason']
                                    t['pending'] = False
                                    t['estimated'] = False
                                    log.info(f"    → Updated pending trade #{ticket} in dashboard with real P&L")
                                    break
                            # Send Discord resolution notification
                            try:
                                acct = executor.get_account_info()
                                bal = acct['balance'] if acct else config.CAPITAL_INICIAL
                                pct = (deal['profit'] / bal) * 100 if bal else 0
                                alert_trade_resolved(
                                    ticket=ticket,
                                    direction=deal.get('direction', direction or '?'),
                                    profit=deal['profit'],
                                    profit_percent=pct,
                                    reason=deal['reason'],
                                )
                            except Exception as e_alert:
                                log.debug(f"    Alert trade resolved error: {e_alert}")
                        else:
                            open_positions = executor.get_open_positions()
                            still_open = any(p.ticket == ticket for p in open_positions)
                            if still_open:
                                # Check staleness: how long since SQLite recorded the close?
                                try:
                                    c2 = sqlite3.connect(db_path, timeout=5)
                                    row = c2.execute(
                                        "SELECT close_time FROM trades WHERE ticket = ?", (ticket,)
                                    ).fetchone()
                                    c2.close()
                                    if row and row[0]:
                                        close_dt = datetime.fromisoformat(row[0])
                                        age_min = (datetime.now() - close_dt).total_seconds() / 60
                                        if age_min > 240:  # >4 hours — stale, try direct lookup
                                            log.warning(
                                                f"    #{ticket}: MT5 says open but closed {age_min:.0f}min ago "
                                                f"— attempting direct deal lookup"
                                            )
                                            direct_deal = get_deal_history(ticket, open_price=open_price or 0)
                                            if direct_deal and not direct_deal.get('pending'):
                                                log.info(
                                                    f"    Resolved #{ticket} (direct): close={direct_deal['close_price']:.2f} | "
                                                    f"P&L=${direct_deal['profit']:+.2f} | {direct_deal['reason']}"
                                                )
                                                record_trade_close(
                                                    ticket=ticket, close_price=direct_deal['close_price'],
                                                    profit=direct_deal['profit'], close_reason=direct_deal['reason'],
                                                    close_time=direct_deal['close_time'].isoformat(),
                                                )
                                                for t in self.closed_trades_today:
                                                    if t.get('ticket') == ticket and t.get('pending'):
                                                        t['profit'] = direct_deal['profit']
                                                        t['close_price'] = direct_deal['close_price']
                                                        t['reason'] = direct_deal['reason']
                                                        t['pending'] = False
                                                        t['estimated'] = False
                                                        log.info(f"    → Updated pending trade #{ticket} in dashboard with real P&L")
                                                        break
                                                try:
                                                    acct = executor.get_account_info()
                                                    bal = acct['balance'] if acct else config.CAPITAL_INICIAL
                                                    pct = (direct_deal['profit'] / bal) * 100 if bal else 0
                                                    alert_trade_resolved(
                                                        ticket=ticket,
                                                        direction=direct_deal.get('direction', direction or '?'),
                                                        profit=direct_deal['profit'],
                                                        profit_percent=pct,
                                                        reason=direct_deal['reason'],
                                                    )
                                                except Exception as e_alert:
                                                    log.debug(f"    Alert trade resolved error: {e_alert}")
                                            else:
                                                log.warning(
                                                    f"    #{ticket}: direct lookup also failed after {age_min:.0f}min "
                                                    f"— possible wrong MT5 terminal or stale data"
                                                )
                                        elif age_min > 60:
                                            log.warning(
                                                f"    #{ticket}: MT5 says open but closed {age_min:.0f}min ago "
                                                f"— possible stale terminal data"
                                            )
                                        else:
                                            log.debug(f"    #{ticket}: still open in MT5 — ignored")
                                    else:
                                        log.debug(f"    #{ticket}: still open in MT5 — ignored")
                                except Exception as e_stale:
                                    log.debug(f"    #{ticket}: staleness check error: {e_stale}")
                                    log.debug(f"    #{ticket}: still open in MT5 — ignored")
                            else:
                                log.info(f"    #{ticket}: no deal in MT5 and not open — unavailable")
                
            except Exception as e:
                log.warning(f"Reconciliation Pass 2/3 error: {e}")
            
            # ================================================================
            # Rebuild daily_stats from closed_trades_today
            # ================================================================
            # Exclude pending trades from stats (they have profit=None)
            confirmed_trades = [t for t in self.closed_trades_today if not t.get('pending', False)]
            pending_trades = [t for t in self.closed_trades_today if t.get('pending', False)]
            new_trades = len(confirmed_trades)
            new_wins = sum(1 for t in confirmed_trades if float(t.get('profit', 0) or 0) > 0)
            new_losses = sum(1 for t in confirmed_trades if float(t.get('profit', 0) or 0) < 0)
            new_breakevens = new_trades - new_wins - new_losses
            new_pnl = sum(float(t.get('profit', 0) or 0) for t in confirmed_trades)
            if pending_trades:
                log.info(f"  {len(pending_trades)} trade(s) still pending P&L confirmation")
            
            self.daily_stats['trades'] = new_trades
            self.daily_stats['wins'] = new_wins
            self.daily_stats['losses'] = new_losses
            self.daily_stats['breakevens'] = new_breakevens
            self.daily_stats['pnl'] = new_pnl
            
            log.info(
                f"Reconciliation complete: balance=${mt5_balance:.2f} | "
                f"Trades today: {new_trades} (W:{new_wins} L:{new_losses}) | "
                f"PnL today: ${new_pnl:+.2f}"
            )
            
        except Exception as e:
            log.warning(f"Reconciliation failed (non-blocking): {e}")

    def _resolve_pending_trades(self) -> None:
        """Periodically attempt to resolve pending trades with real MT5 deal data.
        
        Called every analysis cycle (~300s). When a trade closes and the MT5 API
        doesn't return the closing deal within the retry window, the trade stays
        as 'pending'. This method re-queries MT5 until the deal appears.
        """
        try:
            pending = [t for t in self.closed_trades_today if t.get('pending')]
            if not pending:
                return
            
            log.info(f"Resolving {len(pending)} pending trade(s)...")
            resolved_any = False
            
            for trade in pending:
                ticket = trade.get('ticket')
                if not ticket:
                    continue
                
                deal = get_deal_history(
                    ticket,
                    open_price=trade.get('open_price'),
                    tp_price=trade.get('orig_tp'),
                    sl_price=trade.get('orig_sl'),
                )
                
                if deal and not deal.get('pending'):
                    # Real deal found — resolve
                    trade['profit'] = deal['profit']
                    trade['close_price'] = deal['close_price']
                    trade['reason'] = deal['reason']
                    trade['pending'] = False
                    trade['estimated'] = False
                    resolved_any = True
                    
                    log.info(
                        f"  ✅ Resolved #{ticket}: close={deal['close_price']:.2f} | "
                        f"P&L=${deal['profit']:+.2f} | {deal['reason']}"
                    )
                    
                    # Update SQLite
                    record_trade_close(
                        ticket=ticket, close_price=deal['close_price'],
                        profit=deal['profit'], close_reason=deal['reason'],
                        close_time=deal.get('close_time', datetime.now()).isoformat() if hasattr(deal.get('close_time', ''), 'isoformat') else str(deal.get('close_time', '')),
                    )
                    
                    # Discord notification
                    try:
                        acct = executor.get_account_info()
                        bal = acct['balance'] if acct else config.CAPITAL_INICIAL
                        pct = (deal['profit'] / bal) * 100 if bal else 0
                        alert_trade_resolved(
                            ticket=ticket,
                            direction=trade.get('direction', '?'),
                            profit=deal['profit'],
                            profit_percent=pct,
                            reason=deal['reason'],
                        )
                    except Exception as e_alert:
                        log.debug(f"  Alert trade resolved error: {e_alert}")
                else:
                    log.debug(f"  #{ticket}: deal still not in MT5 history — will retry next cycle")
            
            if resolved_any:
                # Rebuild daily_stats
                confirmed = [t for t in self.closed_trades_today if not t.get('pending', False)]
                still_pending = [t for t in self.closed_trades_today if t.get('pending', False)]
                self.daily_stats['trades'] = len(confirmed)
                self.daily_stats['wins'] = sum(1 for t in confirmed if float(t.get('profit', 0) or 0) > 0)
                self.daily_stats['losses'] = sum(1 for t in confirmed if float(t.get('profit', 0) or 0) < 0)
                self.daily_stats['breakevens'] = self.daily_stats['trades'] - self.daily_stats['wins'] - self.daily_stats['losses']
                self.daily_stats['pnl'] = sum(float(t.get('profit', 0) or 0) for t in confirmed)
                if still_pending:
                    log.info(f"  {len(still_pending)} trade(s) still pending")
                write_state(self)
                
        except Exception as e:
            log.debug(f"Resolve pending trades error (non-blocking): {e}")

    def _launch_dashboard_server(self) -> None:
        try:
            base_dir = os.path.dirname(os.path.abspath(__file__))
            dashboard_dir = os.path.join(base_dir, "dashboard")
            server_py = os.path.join(dashboard_dir, "server.py")
            if not os.path.exists(server_py):
                return

            env = os.environ.copy()
            env["DASHBOARD_STATE_FILE"] = os.path.abspath(getattr(config, "DASHBOARD_STATE_FILE", "data/bot_state.json"))

            cmd = [
                sys.executable,
                "-m",
                "uvicorn",
                "dashboard.server:app",
                "--host",
                "127.0.0.1",
                "--port",
                "8080",
            ]

            creationflags = 0
            if os.name == "nt":
                creationflags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)

            subprocess.Popen(
                cmd,
                cwd=base_dir,
                env=env,
                creationflags=creationflags,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception as e:
            log.debug(f"Failed to launch dashboard server: {e}")
    
    def start(self):
        """Start the bot"""
        self.session_start_time = datetime.now()
        self.session_analyses = 0

        self._load_persisted_state()
        init_db()
        
        log.info("")
        log.info("=" * 60)
        log.info("🚀 SESSION START")
        log.info("=" * 60)
        log.info(f"Timestamp: {self.session_start_time.strftime('%Y-%m-%d %H:%M:%S')}")
        
        mode_labels = {
            "DRY_RUN": "DRY RUN (pure simulation)",
            "DEMO": "DEMO MT5 (real execution, fake $)",
            "LIVE": "LIVE (real execution, real $)",
        }
        mode_label = mode_labels.get(self.mode, self.mode)
        log.info(f"Mode: {mode_label}")
        log.info(f"Symbol: {config.SYMBOL} | Timeframe: {config.TIMEFRAME}")
        log.info(f"Analysis: {config.ANALYSIS_INTERVAL_SECONDS}s | Monitor: {config.MONITOR_INTERVAL_SECONDS}s")
        log.info(f"Central Brain: {'ON' if config.USE_CENTRAL_BRAIN else 'OFF (confluence)'}")
        log.info(f"GPT Headlines: {'ON (' + config.GPT_MODEL + ')' if getattr(config, 'USE_GPT_HEADLINES', False) else 'OFF (keywords)'}")
        log.info(f"Min confidence: {config.BRAIN_MIN_CONFIDENCE}%")
        
        # Initialize AI Agent (if enabled)
        if getattr(config, 'USE_AI_AGENT', False):
            try:
                from ai_agent import initialize_agent, get_agent
                if initialize_agent():
                    agent = get_agent()
                    log.info(f"AI Agent: ON (mode={agent.get_mode()}, model={config.AI_AGENT_MODEL})")
                else:
                    log.info("AI Agent: OFF (initialization failed)")
            except Exception as e:
                log.warning(f"AI Agent: OFF (error: {e})")
        else:
            log.info("AI Agent: OFF")
        log.info(f"Risk/trade: {config.RISK_PER_TRADE}% | Max daily loss: {config.MAX_DAILY_LOSS}%")
        log.info(f"SL: {config.MIN_SL_PIPS}-{config.MAX_SL_PIPS} pips | Breakeven: {int(config.BREAKEVEN_ATR_MULT * 100)}% of SL (dynamic) | Trailing: {int(config.TRAILING_ATR_MULT * 100)}% of SL")
        
        # Connect MT5
        if self.executes_trades:
            # DEMO and LIVE: full connection with order execution
            if not connect_mt5():
                log.error("Failed to connect MT5. Aborting.")
                alert_error("Startup Failed", "Could not connect to MT5")
                return False
            
            account_info = executor.get_account_info()
            if account_info:
                log.info(f"Account: {account_info['login']}")
                log.info(f"Balance: ${account_info['balance']:.2f}")
                log.info(f"Leverage: 1:{account_info['leverage']}")
                if self.mode == "DEMO":
                    log.info("⚠️ DEMO MODE - Real orders on DEMO account (fake money)")
        else:
            log.info("DRY RUN mode - MT5 will not be connected for real orders")
            # Initialize MT5 for data only
            import MetaTrader5 as mt5
            if not mt5.initialize():
                log.warning("MT5 not available for data. Using simulated data.")
        
        # Clear stale signal file (prevents 4-day-old signals from reaching EA on restart)
        if getattr(config, 'USE_EA_BRIDGE', False):
            try:
                from ea_bridge import clear_stale_signal
                clear_stale_signal(max_age_hours=4.0)
            except Exception as e:
                log.debug(f"Stale signal cleanup skipped: {e}")
        
        # Reconcile saved state with MT5 (fix trades that closed during downtime)
        self._reconcile_with_mt5()
        
        # Send Discord alert
        alert_bot_started(mode_label)
        
        self._launch_dashboard_server()

        self.running = True
        log.success("Bot started successfully!")
        
        write_state(self)
        
        return True
    
    def stop(self, reason: str = "Manual"):
        """Stop the bot"""
        self.running = False

        write_state(self)
        
        # Calculate runtime
        stop_time = datetime.now()
        runtime = stop_time - self.session_start_time if self.session_start_time else timedelta(0)
        hours, remainder = divmod(int(runtime.total_seconds()), 3600)
        minutes, seconds = divmod(remainder, 60)
        runtime_str = f"{hours}h {minutes}m {seconds}s"
        
        # Send daily summary
        self._send_daily_summary()
        
        # Disconnect MT5
        if self.executes_trades:
            disconnect_mt5()
        
        # Discord alert
        alert_bot_stopped(reason)
        
        # Log session summary
        stats = self.daily_stats
        log.info("")
        log.info("=" * 60)
        log.info("🛑 SESSION STOP")
        log.info("=" * 60)
        log.info(f"Reason: {reason}")
        log.info(f"Start:   {self.session_start_time.strftime('%Y-%m-%d %H:%M:%S') if self.session_start_time else 'N/A'}")
        log.info(f"End:     {stop_time.strftime('%Y-%m-%d %H:%M:%S')}")
        log.info(f"Duration: {runtime_str}")
        log.info(f"Analyses performed: {self.session_analyses}")
        log.info(f"Trades: {stats['trades']} (W:{stats['wins']} L:{stats['losses']})")
        log.info(f"Session PnL: ${stats['pnl']:+.2f}")
        gpt = self.gpt_stats
        log.info(f"GPT Validator: CONFIRM:{gpt['confirm']} BOOST:{gpt['boost']} REDUCE:{gpt['reduce']} (cache:{gpt['from_cache']})")
        log.info("=" * 60)
        log.info("")
    
    def run(self):
        """Main bot loop"""
        if not self.start():
            return
        
        log.info("Entering main loop...")
        
        while self.running:
            try:
                # Daily reset
                self._check_daily_reset()
                
                # Check if market is open
                market_open, market_reason, next_open = is_market_open()
                
                if not market_open:
                    # === MARKET CLOSED ===
                    # Transition: open → closed (send alert once)
                    if self.market_was_open:
                        self.market_was_open = False
                        next_open_str = next_open.strftime('%Y-%m-%d %H:%M UTC') if next_open else "unknown"
                        log.info(f"🌙 Market closed: {market_reason}")
                        log.info(f"   Next open: {next_open_str}")
                        alert_market_closed(market_reason, f"Next open: {next_open_str}")
                    
                    # Monitor continues managing existing positions (trailing, breakeven)
                    self._monitor_cycle()

                    write_state(self)
                    
                    # Differential sleep: daily pause (60s) vs weekend (300s)
                    is_weekend = "Weekend" in market_reason
                    sleep_seconds = 300 if is_weekend else 60
                    
                    for _ in range(sleep_seconds):
                        if not self.running:
                            break
                        time.sleep(1)
                    
                    # Periodic keepalive log (to show the bot is alive)
                    keepalive_interval = 3600 if is_weekend else 600  # 1h weekend, 10 min daily pause
                    now = datetime.now()
                    if self._last_keepalive_log is None or (now - self._last_keepalive_log).total_seconds() >= keepalive_interval:
                        next_open_str = next_open.strftime('%Y-%m-%d %H:%M UTC') if next_open else "unknown"
                        close_type = "weekend" if is_weekend else "daily pause"
                        log.info(f"💤 Market closed ({close_type}). Next open: {next_open_str}")
                        self._last_keepalive_log = now
                    
                    continue
                
                # === MARKET OPEN ===
                # Transition: closed → open (send alert once)
                if not self.market_was_open:
                    self.market_was_open = True
                    self._last_keepalive_log = None
                    log.info("☀️ Market open! Bot active.")
                    alert_market_open()
                
                # Execute analysis cycle
                self._analysis_cycle()
                self.session_analyses += 1
                
                # Monitor open positions
                self._monitor_cycle()
                
                # Wait for next cycle with monitor sub-loop
                # If positions open: monitor every 30s
                # If not: normal 300s sleep
                elapsed = 0
                interval = config.ANALYSIS_INTERVAL_SECONDS
                monitor_interval = config.MONITOR_INTERVAL_SECONDS
                
                while elapsed < interval and self.running:
                    # Sleep for monitor interval or remaining time
                    sleep_time = min(monitor_interval, interval - elapsed)
                    for _ in range(sleep_time):
                        if not self.running:
                            break
                        time.sleep(1)
                    elapsed += sleep_time
                    
                    # If not yet time for next analysis, run monitor
                    if elapsed < interval and self.running and self.executes_trades:
                        positions = executor.get_open_positions()
                        if positions:
                            log.debug(f"Monitor tick: {len(positions)} open position(s) ({elapsed}s/{interval}s)")
                            self._monitor_cycle()
                
            except KeyboardInterrupt:
                log.info("User interruption...")
                break
                
            except Exception as e:
                log.error(f"Error in main loop: {e}")
                log.error(traceback.format_exc())
                alert_error("Loop Error", str(e))
                
                # Wait before retrying
                time.sleep(60)
        
        self.stop("Loop ended")
    
    def _analysis_cycle(self):
        """Analysis and decision cycle"""
        try:
            log.info("-" * 40)
            log.info(f"📊 Analysis: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
            
            # Resolve any pending trades from previous cycles
            self._resolve_pending_trades()
            
            # 1. Get technical data
            df = get_mt5_data()
            
            if df is None or len(df) < 50:
                log.warning("Insufficient data for analysis")
                return
            
            # 2. Calculate indicators
            df = calculate_indicators(df)
            
            # ================================================================
            # CENTRAL BRAIN (or fallback to confluence)
            # ================================================================
            if config.USE_CENTRAL_BRAIN:
                try:
                    decision, final_score, confidence, direction, tech_score, news_score, ml_score, explanation, agent_data = \
                        self._brain_analysis(df)
                except Exception as e:
                    log.error(f"⚠️ Brain failed! Error: {e}")
                    log.error(traceback.format_exc())
                    log.warning("Using confluence as fallback...")
                    alert_error("Brain Degraded", f"Brain failed: {e}. Using confluence as fallback.")
                    decision, final_score, confidence, direction, tech_score, news_score, ml_score, explanation, agent_data = \
                        self._confluence_analysis(df)
            else:
                decision, final_score, confidence, direction, tech_score, news_score, ml_score, explanation, agent_data = \
                    self._confluence_analysis(df)

            # ================================================================
            # PROACTIVE AI AGENT (H1 snapshot) — shadow mode, diagnostic only
            # Runs once per closed H1 candle when market is open.
            # ================================================================
            if getattr(config, 'USE_AI_AGENT', False):
                try:
                    market_open, _, _ = is_market_open()
                    if market_open:
                        last_closed_h1_iso = self._get_last_closed_h1_time_iso()
                        if last_closed_h1_iso:
                            prev_iso = getattr(self, '_last_proactive_h1_close_time', None)
                            if prev_iso != last_closed_h1_iso:
                                self._call_agent_proactive_h1_snapshot(
                                    h1_close_time_iso=last_closed_h1_iso,
                                    agent_data=agent_data,
                                    df=df,
                                )
                                self._last_proactive_h1_close_time = last_closed_h1_iso
                except Exception as e:
                    log.warning(f"PROACTIVE_H1 | error (non-blocking): {e}")
            
            if decision is None:
                return

            hold_forced = False
            original_decision = None
            hold_reason = None
            last_analysis = self.last_analysis or {}
            if isinstance(last_analysis, dict):
                hold_forced = bool(last_analysis.get("hold_forced"))
                original_decision = last_analysis.get("original_decision")
                hold_reason = last_analysis.get("hold_reason")
            
            # Analysis log
            log.analysis(tech_score, news_score, ml_score, final_score)
            if direction is None and decision != "HOLD":
                log.decision(f"FORCED HOLD ({decision} blocked: conf {confidence:.1f}% < {config.BRAIN_MIN_CONFIDENCE}%)", confidence, final_score)
            else:
                log.decision(decision, confidence, final_score)
            
            if config.USE_CENTRAL_BRAIN:
                self._check_heartbeat()

            # Check if actionable signal
            if direction is None:
                log.info(f"   Decision: {decision} - Waiting...")
                if config.USE_CENTRAL_BRAIN and hold_forced:
                    alert_brain_decision(
                        decision=decision,
                        final_score=final_score,
                        confidence=confidence,
                        scenario=last_analysis.get("scenario", ""),
                        tech_score=last_analysis.get("tech_score", tech_score),
                        ml_score=last_analysis.get("ml_score", ml_score),
                        momentum_score=last_analysis.get("momentum_score", 50.0),
                        news_score=last_analysis.get("news_score", news_score),
                        calendar_score=last_analysis.get("calendar_score", 50.0),
                        gpt_validation=last_analysis.get("gpt_validation"),
                        volatility_status=last_analysis.get("volatility_status", "NORMAL"),
                        mtf_trend=last_analysis.get("mtf_trend"),
                        volume_gate=last_analysis.get("volume_gate"),
                        hold_forced=True,
                        original_decision=original_decision,
                        hold_reason=hold_reason,
                    )
                    # AI Agent for HOLD_FORCED: Agent can evaluate if Brain was right to block
                    if getattr(config, 'USE_AI_AGENT', False) and agent_data is not None:
                        try:
                            self._call_agent_shadow_mode(
                                brain_result=agent_data["brain_result"],
                                tech_data=agent_data["tech_data"],
                                ml_data=agent_data["ml_data"],
                                momentum_data=agent_data["momentum_data"],
                                news_data=agent_data["news_data"],
                                calendar_data=agent_data["calendar_data"],
                                current_price=agent_data["current_price"],
                                vol_status=agent_data["vol_status"],
                                df=agent_data["df"],
                                hold_forced=agent_data["hold_forced"],
                                original_decision=agent_data["original_decision"],
                                hold_reason=agent_data["hold_reason"],
                            )
                        except Exception as e:
                            log.warning(f"AI Agent error (non-blocking): {e}")
                return
            
            # Signal detected!
            log.info(f"   🔔 SIGNAL: {decision} ({direction})")

            if config.USE_CENTRAL_BRAIN:
                alert_brain_decision(
                    decision=decision,
                    final_score=final_score,
                    confidence=confidence,
                    scenario=last_analysis.get("scenario", ""),
                    tech_score=last_analysis.get("tech_score", tech_score),
                    ml_score=last_analysis.get("ml_score", ml_score),
                    momentum_score=last_analysis.get("momentum_score", 50.0),
                    news_score=last_analysis.get("news_score", news_score),
                    calendar_score=last_analysis.get("calendar_score", 50.0),
                    gpt_validation=last_analysis.get("gpt_validation"),
                    volatility_status=last_analysis.get("volatility_status", "NORMAL"),
                    mtf_trend=last_analysis.get("mtf_trend"),
                    volume_gate=last_analysis.get("volume_gate"),
                )

            atr = get_atr_value(df)
            prices = executor.get_current_price()
            if prices:
                entry_price = prices[1] if direction == "BUY" else prices[0]
            else:
                entry_price = df['close'].iloc[-1]

            levels = calculate_sl_tp(entry_price, direction, atr)
            
            # Alert Discord
            alert_signal_detected(
                decision=decision,
                final_score=final_score,
                tech_score=tech_score,
                news_score=news_score,
                ml_score=ml_score,
                confidence=confidence,
                brain_summary=explanation,
                current_price=entry_price,
                stop_loss=levels.stop_loss,
                take_profit=levels.take_profit_1,
                scenario=last_analysis.get("scenario", None),
            )
            
            # Safety Checks
            positions_list = get_positions() if self.executes_trades else []
            account_balance = get_account_balance() if self.executes_trades else config.CAPITAL_INICIAL
            open_positions = len(positions_list)
            mt5_connected = is_mt5_connected() if self.executes_trades else True
            
            is_safe, reasons = is_safe_to_trade(
                account_balance=account_balance,
                open_positions=open_positions,
                mt5_connected=mt5_connected,
                has_high_impact_news=False,
                trade_direction=direction,
                open_positions_list=positions_list
            )
            
            if not is_safe:
                reason_str = "; ".join(reasons)
                log.safety_block(reason_str)
                alert_safety_block(decision, final_score, reason_str)
                return
            
            # M5 Reversal Detection (anti-lag filter)
            try:
                from momentum_detector import check_m5_reversal
                m5_check = check_m5_reversal(direction)
                log.info(f"   🔄 M5 Check: {m5_check['description']}")
                
                if m5_check["reversal_detected"]:
                    if m5_check["reversal_strength"] == "strong":
                        log.safety_block(f"Strong M5 reversal: {m5_check['description']}")
                        alert_m5_reversal_block(direction, m5_check["recent_move_pct"], m5_check["description"])
                        return
                    elif m5_check["reversal_strength"] == "moderate":
                        confidence -= config.M5_REVERSAL_CONFIDENCE_PENALTY
                        log.info(f"   ⚠️ Moderate M5 reversal: confidence reduced {config.M5_REVERSAL_CONFIDENCE_PENALTY} → {confidence:.1f}")
                        if confidence < config.BRAIN_MIN_CONFIDENCE:
                            log.safety_block(f"Moderate M5 reversal reduced confidence below minimum ({confidence:.1f} < {config.BRAIN_MIN_CONFIDENCE})")
                            alert_m5_reversal_block(direction, m5_check["recent_move_pct"], m5_check["description"])
                            return
            except Exception as e:
                log.warning(f"M5 reversal check error (ignored): {e}")
            
            # AI Agent Shadow Mode: Call Agent AFTER safety checks pass
            # This ensures Agent is not called when safety blocks the trade
            if getattr(config, 'USE_AI_AGENT', False) and agent_data is not None:
                try:
                    self._call_agent_shadow_mode(
                        brain_result=agent_data["brain_result"],
                        tech_data=agent_data["tech_data"],
                        ml_data=agent_data["ml_data"],
                        momentum_data=agent_data["momentum_data"],
                        news_data=agent_data["news_data"],
                        calendar_data=agent_data["calendar_data"],
                        current_price=agent_data["current_price"],
                        vol_status=agent_data["vol_status"],
                        df=agent_data["df"],
                        hold_forced=agent_data["hold_forced"],
                        original_decision=agent_data["original_decision"],
                        hold_reason=agent_data["hold_reason"],
                    )
                except Exception as e:
                    log.warning(f"AI Agent error (non-blocking): {e}")
            
            # Spread Check with Retry Loop
            spread = executor.get_spread()
            if spread is not None:
                log.info(f"   📊 Spread: {spread:.1f} pips")
                
                if spread > config.MAX_SPREAD_PIPS:
                    log.warning(f"   ⏳ Spread too high: {spread:.1f} pips (max: {config.MAX_SPREAD_PIPS}) — delaying entry")
                    alert_spread_delay(spread, config.MAX_SPREAD_PIPS, 1)
                    
                    # Retry loop
                    for retry in range(2, config.SPREAD_MAX_RETRIES + 1):
                        time.sleep(config.SPREAD_RETRY_INTERVAL_SECONDS)
                        spread = executor.get_spread()
                        
                        if spread is None:
                            log.warning(f"   ⚠️ Could not get spread on retry #{retry}")
                            continue
                        
                        log.info(f"   📊 Spread retry #{retry}: {spread:.1f} pips")
                        
                        if spread <= config.MAX_SPREAD_PIPS:
                            log.info(f"   ✅ Spread normalized: {spread:.1f} pips — proceeding with entry")
                            break
                    else:
                        # Exhausted all retries
                        log.warning(f"   ⛔ Spread did not normalize after {config.SPREAD_MAX_RETRIES} retries — trade skipped")
                        alert_spread_skip(direction, spread if spread else 0, final_score)
                        return
            else:
                log.warning("   ⚠️ Could not get spread — proceeding anyway")
            
            # Calculate risk
            sl_pips = levels.sl_pips
            pos_size = calculate_position_size(account_balance, config.RISK_PER_TRADE, sl_pips)
            
            log.info(f"   Entry: {entry_price:.2f}")
            log.info(f"   SL: {levels.stop_loss:.2f} ({levels.sl_pips:.0f} pips)")
            log.info(f"   TP1: {levels.take_profit_1:.2f} ({levels.tp1_pips:.0f} pips)")
            log.info(f"   Lot: {pos_size.lot_size}")
            
            # Execute trade
            comment = f"Bot-{decision}-{final_score:.0f}"
            
            # Determine trailing parameters (Volatility Guard may override)
            sl_pips_orig = levels.sl_pips
            if self._last_vol_status == "COOLING_DOWN":
                be_trigger = config.COOLING_BREAKEVEN_TRIGGER_PIPS
                tr_trigger = config.COOLING_TRAILING_TRIGGER_PIPS
                tr_distance = config.COOLING_TRAILING_DISTANCE_PIPS
            else:
                be_trigger = sl_pips_orig * getattr(config, 'BREAKEVEN_ATR_MULT', 0.7)
                tr_trigger = sl_pips_orig * getattr(config, 'TRAILING_ATR_MULT', 0.7)
                tr_distance = sl_pips_orig * getattr(config, 'TRAILING_DISTANCE_ATR_MULT', 0.7)
            
            # Check if EA bridge is enabled and EA is online
            use_ea = False
            if getattr(config, 'USE_EA_BRIDGE', False) and self.executes_trades:
                try:
                    from ea_bridge import is_ea_online, write_signal
                    stale_threshold = getattr(config, 'EA_STALE_THRESHOLD_SECONDS', 60)
                    if is_ea_online(stale_threshold):
                        use_ea = True
                        log.info(f"   🔗 EA Bridge: ONLINE — sending signal via JSON")
                    else:
                        log.warning(f"   ⚠️ EA Bridge: OFFLINE — falling back to direct MT5 API")
                except Exception as e:
                    log.warning(f"   ⚠️ EA Bridge error: {e} — falling back to direct MT5 API")
            
            if use_ea:
                # EA Bridge: Write signal to JSON, EA handles execution
                signal_ok = write_signal(
                    signal=direction,
                    sl=levels.stop_loss,
                    tp=levels.take_profit_1,
                    lot_size=pos_size.lot_size,
                    confidence=confidence,
                    breakeven_trigger_pips=be_trigger,
                    trailing_trigger_pips=tr_trigger,
                    trailing_distance_pips=tr_distance,
                    max_drawdown_pips=config.MAX_POSITION_DRAWDOWN_PIPS,
                    comment=comment
                )
                
                if signal_ok:
                    log.success(f"Signal sent to EA: {direction}")
                    self.daily_stats['trades'] += 1
                    record_trade_opened(direction)
                    # Note: ticket will be recorded when EA confirms execution
                    # For now, record with placeholder ticket (EA will update)
                    record_trade_open(
                        ticket=0,  # EA will assign real ticket
                        direction=direction,
                        volume=pos_size.lot_size,
                        open_price=entry_price,
                        sl=levels.stop_loss,
                        tp=levels.take_profit_1,
                        comment=comment,
                    )
                else:
                    log.error(f"Failed to send signal to EA")
            else:
                # Direct MT5 API execution (fallback or EA disabled)
                trade_confidence = last_analysis.get("confidence", confidence)
                trade_scenario = last_analysis.get("scenario", None)
                trade_risk_amount = pos_size.risk_amount if pos_size else None
                trade_risk_percent = config.RISK_PER_TRADE

                if direction == "BUY":
                    order_result = execute_buy(
                        lot_size=pos_size.lot_size,
                        sl=levels.stop_loss,
                        tp=levels.take_profit_1,
                        comment=comment,
                        confidence=trade_confidence,
                        scenario=trade_scenario,
                        risk_amount=trade_risk_amount,
                        risk_percent=trade_risk_percent,
                    )
                else:
                    order_result = execute_sell(
                        lot_size=pos_size.lot_size,
                        sl=levels.stop_loss,
                        tp=levels.take_profit_1,
                        comment=comment,
                        confidence=trade_confidence,
                        scenario=trade_scenario,
                        risk_amount=trade_risk_amount,
                        risk_percent=trade_risk_percent,
                    )
                
                if order_result.success:
                    log.success(f"Trade executed! Ticket: {order_result.ticket}")
                    self.daily_stats['trades'] += 1
                    record_trade_opened(direction)
                    record_trade_open(
                        ticket=order_result.ticket,
                        direction=direction,
                        volume=pos_size.lot_size,
                        open_price=order_result.price,
                        sl=levels.stop_loss,
                        tp=levels.take_profit_1,
                        comment=comment,
                    )
                else:
                    log.error(f"Failed to execute trade: {order_result.error_message}")
        finally:
            # Check EA Bridge status and alert if offline (must never block the bot)
            check_ea_bridge_status_and_alert()
            # Persist state for dashboard (must never block the bot)
            write_state(self)

    def _get_last_closed_h1_time_iso(self) -> str:
        """Return ISO timestamp of the last CLOSED H1 candle, or empty string if unavailable."""
        try:
            import MetaTrader5 as mt5
            rates = mt5.copy_rates_from_pos(config.SYMBOL, mt5.TIMEFRAME_H1, 1, 1)
            if rates is None or len(rates) == 0:
                return ""
            t = int(rates[0]["time"])
            return datetime.fromtimestamp(t, tz=timezone.utc).isoformat()
        except Exception:
            return ""

    def _call_agent_proactive_h1_snapshot(self, h1_close_time_iso: str, agent_data: dict, df):
        """Call the AI Agent proactively once per H1 candle close (diagnostic only)."""
        import asyncio
        from ai_agent import get_agent
        from agent_data_builder import get_session_name
        from db_writer import record_agent_proactive_analysis
        from ai_agent import agent_decide
        from agent_data_builder import build_data_package

        agent = get_agent()
        if not agent.is_enabled():
            return

        if not agent_data or not isinstance(agent_data, dict):
            return

        log.info(f"PROACTIVE_H1 | Calling AI Agent (shadow snapshot) | H1 close: {h1_close_time_iso}")

        try:
            # Reuse the same full payload used by reactive calls
            brain_result = agent_data.get("brain_result")
            tech_data = agent_data.get("tech_data")
            ml_data = agent_data.get("ml_data")
            momentum_data = agent_data.get("momentum_data")
            news_data = agent_data.get("news_data")
            calendar_data = agent_data.get("calendar_data")
            current_price = agent_data.get("current_price")
            vol_status = agent_data.get("vol_status")

            session_context = {
                "session_name": get_session_name(datetime.utcnow().hour),
                "hour_utc": datetime.utcnow().hour,
                "today_trades": self.daily_stats.get("trades", 0),
                "today_wins": self.daily_stats.get("wins", 0),
                "today_losses": self.daily_stats.get("losses", 0),
                "today_pnl": self.daily_stats.get("pnl", 0),
                "last_5_results": [],
                "consecutive_losses": 0,
                "hold_forced": None,
            }

            h1_candles = []
            for i in range(max(0, len(df) - 20), len(df)):
                row = df.iloc[i]
                h1_candles.append({
                    "time": str(row.get("datetime", "")),
                    "open": float(row["open"]),
                    "high": float(row["high"]),
                    "low": float(row["low"]),
                    "close": float(row["close"]),
                    "tick_volume": int(row.get("tick_volume", 0)),
                })

            m5_candles = []
            d1_candles = []
            h4_candles = []
            try:
                import MetaTrader5 as mt5
                m5_rates = mt5.copy_rates_from_pos(config.SYMBOL, mt5.TIMEFRAME_M5, 0, 10)
                if m5_rates is not None:
                    for r in m5_rates:
                        m5_candles.append({
                            "time": datetime.fromtimestamp(r["time"], tz=timezone.utc).isoformat(),
                            "open": float(r["open"]),
                            "high": float(r["high"]),
                            "low": float(r["low"]),
                            "close": float(r["close"]),
                            "tick_volume": int(r["tick_volume"]),
                        })
                d1_rates = mt5.copy_rates_from_pos(config.SYMBOL, mt5.TIMEFRAME_D1, 0, 10)
                if d1_rates is not None:
                    for r in d1_rates:
                        d1_candles.append({
                            "time": datetime.fromtimestamp(r["time"], tz=timezone.utc).isoformat(),
                            "open": float(r["open"]),
                            "high": float(r["high"]),
                            "low": float(r["low"]),
                            "close": float(r["close"]),
                            "tick_volume": int(r["tick_volume"]),
                        })
                h4_rates = mt5.copy_rates_from_pos(config.SYMBOL, mt5.TIMEFRAME_H4, 0, 15)
                if h4_rates is not None:
                    for r in h4_rates:
                        h4_candles.append({
                            "time": datetime.fromtimestamp(r["time"], tz=timezone.utc).isoformat(),
                            "open": float(r["open"]),
                            "high": float(r["high"]),
                            "low": float(r["low"]),
                            "close": float(r["close"]),
                            "tick_volume": int(r["tick_volume"]),
                        })
            except Exception:
                pass

            price_data = {"bid": current_price, "ask": current_price, "spread": 0}
            try:
                tick = executor.get_current_price()
                if tick:
                    price_data = {"bid": tick[0], "ask": tick[1], "spread": (tick[1] - tick[0]) / 0.1}
            except Exception:
                pass

            positions = []
            try:
                if self.executes_trades:
                    pos_list = executor.get_open_positions()
                    for p in pos_list[:3]:
                        positions.append({
                            "ticket": p.ticket,
                            "type": "BUY" if p.type == 0 else "SELL",
                            "price_open": p.price_open,
                            "price_current": p.price_current,
                            "profit": p.profit,
                            "sl": p.sl,
                            "tp": p.tp,
                        })
            except Exception:
                pass

            sr_zones_for_agent = getattr(self, '_last_sr_zones', []) or []
            candlestick_patterns_for_agent = getattr(self, '_last_candlestick_patterns', None)

            sr_proximity_data = None
            try:
                sr_brain_data = getattr(self, '_last_sr_brain_data', None)
                if sr_brain_data:
                    sr_proximity_data = {
                        "near_strong_zone": sr_brain_data.get("near_strong_zone", False),
                        "near_zone_info": sr_brain_data.get("near_zone_info"),
                        "dist_to_nearest_pips": sr_brain_data.get("dist_to_nearest_pips"),
                    }
            except Exception:
                pass

            agent_memory = []
            trade_feedback = None
            delta_context = None
            portfolio_data = None
            regime_context = None

            data_package = build_data_package(
                brain_result=brain_result,
                tech_data=tech_data,
                ml_data=ml_data,
                momentum_data=momentum_data,
                news_data=news_data,
                calendar_data=calendar_data,
                h1_candles=h1_candles,
                m5_candles=m5_candles,
                current_price=price_data,
                positions=positions,
                session_context=session_context,
                volatility_status=vol_status or {},
                sr_zones=sr_zones_for_agent,
                candlestick_patterns=candlestick_patterns_for_agent,
                sr_proximity=sr_proximity_data,
                d1_candles=d1_candles,
                h4_candles=h4_candles,
                agent_memory=agent_memory,
                trade_feedback=trade_feedback,
                delta_context=delta_context,
                portfolio=portfolio_data,
                regime_context=regime_context,
            )

            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            agent_result = loop.run_until_complete(
                agent_decide(
                    data_package,
                    trigger_type="PROACTIVE_H1",
                    allow_memory_write=False,
                )
            )
            loop.close()
        except Exception as e:
            log.warning(f"PROACTIVE_H1 | Agent call failed (non-blocking): {e}")
            return

        try:
            # Persist to SQLite
            record_agent_proactive_analysis(h1_close_time_iso, agent_result.to_dict())
        except Exception as e:
            log.warning(f"PROACTIVE_H1 | DB write error (ignored): {e}")

        try:
            # Store for dashboard
            if self.last_analysis and isinstance(self.last_analysis, dict):
                self.last_analysis["proactive_analysis"] = {
                    "trigger": "PROACTIVE_H1",
                    "h1_close_time": h1_close_time_iso,
                    "timestamp": agent_result.timestamp.isoformat() if agent_result.timestamp else datetime.utcnow().isoformat(),
                    "model": agent_result.model,
                    "prompt_version": agent_result.prompt_version,
                    "prompt_hash": agent_result.prompt_hash,
                    "decision": agent_result.decision,
                    "confidence": agent_result.confidence,
                    "reasoning": agent_result.reasoning,
                    "key_factors": agent_result.key_factors,
                    "concerns": agent_result.concerns,
                    "latency_ms": agent_result.latency_ms,
                    "input_tokens": agent_result.input_tokens,
                    "output_tokens": agent_result.output_tokens,
                    "tokens_used": (agent_result.input_tokens or 0) + (agent_result.output_tokens or 0),
                }
        except Exception as e:
            log.debug(f"PROACTIVE_H1 | state update error (ignored): {e}")

        try:
            log.info(
                f"PROACTIVE_H1 | Agent decision: {agent_result.decision} | "
                f"conf={agent_result.confidence} | H1 close: {h1_close_time_iso}"
            )
        except Exception:
            pass
    
    def _brain_analysis(self, df):
        """
        Analysis via Central Brain.
        
        Returns:
            Tuple: (decision, final_score, confidence, direction, tech_score, news_score, ml_score, explanation)
            direction is None if not actionable
        """
        from technical_analyzer import analyze_technical_detailed
        from ml_predictor import get_ml_detailed, set_news_data_for_ml
        from news_score_hybrid import get_news_detailed, get_hybrid_score_cached
        from momentum_detector import analyze_momentum
        from central_brain import analyze_with_brain, is_actionable_signal, get_trade_direction
        from economic_calendar import get_calendar_data, get_upcoming_events
        from volatility_guard import get_volatility_status
        
        log.info("   🧠 Mode: CENTRAL BRAIN")
        
        # Detailed technical analysis
        tech_data = analyze_technical_detailed(df)
        log.info(f"   Technical: {tech_data['score']:.1f}/100")
        
        # Detailed news (fetched BEFORE ML — ML uses DXY/VIX/Yields as features)
        try:
            news_data = get_news_detailed()
        except Exception as e:
            log.warning(f"News error: {e}")
            news_data = {
                "score": 50.0, "dxy": {}, "yields": {}, "vix": {},
                "sentiment": {"headlines_score": 50, "normalized": 0},
                "high_impact_news_soon": False, "geopolitical_risk": "low",
                "anomalies": [], "error": str(e),
            }
        dxy_val = news_data.get('dxy', {}).get('value')
        yields_val = news_data.get('yields', {}).get('value')
        vix_val = news_data.get('vix', {}).get('value')
        news_extra = []
        if dxy_val is not None:
            news_extra.append(f"DXY: {dxy_val}")
        if yields_val is not None:
            news_extra.append(f"10Y: {yields_val}%")
        if vix_val is not None:
            news_extra.append(f"VIX: {vix_val}")
        news_suffix = f" ({', '.join(news_extra)})" if news_extra else ""
        log.info(f"   News: {news_data['score']:.1f}/100{news_suffix}")
        
        # Cache news_data for ML (used by get_ml_score elsewhere)
        set_news_data_for_ml(news_data)
        
        # Detailed ML (uses news_data for DXY/VIX/Yields features)
        try:
            ml_data = get_ml_detailed(df, news_data)
        except Exception as e:
            log.warning(f"ML error: {e}")
            ml_data = {
                "score": 50.0, "prediction": "neutral", "probability": 0.5,
                "max_confidence": 0.5, "pattern": "indefinido",
                "similar_patterns_count": None, "historical_success_rate": None,
                "error": str(e),
            }
        ml_h1 = ml_data.get('score_h1', ml_data['score'])
        ml_h4 = ml_data.get('score_h4', ml_data['score'])
        log.info(f"   ML: {ml_data['score']:.1f}/100 (H1: {ml_h1:.1f}, H4: {ml_h4:.1f}, blend 40/60) ({ml_data['prediction']}, conf: {ml_data['max_confidence']:.0%})")
        
        # Momentum
        momentum_data = analyze_momentum(df)
        log.info(f"   Momentum: {momentum_data['score']:.1f}/100")
        
        # Economic Calendar (5th pillar)
        try:
            calendar_data = get_calendar_data()
        except Exception as e:
            log.warning(f"Calendar error: {e}")
            calendar_data = {
                "score": 50.0, "bias": "NEUTRAL", "phase": "normal",
                "phase_description": "Calendar error - neutral mode",
                "events": [], "events_count": 0, "closest_event": None,
                "source": "error_fallback", "error": str(e),
            }
        log.info(f"   Calendar: {calendar_data['score']:.1f}/100 (phase: {calendar_data['phase']}, bias: {calendar_data['bias']}, source: {calendar_data['source']})")
        
        # Volatility Guard
        try:
            vol_status = get_volatility_status()
        except Exception as e:
            log.warning(f"Volatility Guard error: {e}")
            vol_status = {
                "status": "NORMAL", "last_extreme_candle": None,
                "minutes_since_extreme": None, "extreme_percent": 0,
                "description": f"Error: {e} — neutral mode",
            }
        log.info(f"   Volatility: {vol_status['status']} ({vol_status['description']})")
        
        # M5 Status (visibility in all cycles + input for score adjustment)
        m5_status = None
        try:
            from momentum_detector import get_m5_status
            m5_status = get_m5_status()
            log.info(f"   M5: {m5_status['description']}")
        except Exception as e:
            log.debug(f"   M5 status error: {e}")
        
        # Update monitor with volatility status
        from monitor import monitor as _monitor_instance
        _monitor_instance.set_volatility_status(vol_status['status'])
        
        # Current price
        # Prefer MT5 tick (real-time) when market open and available;
        # fallback to last candle close (H1) when market closed or tick unavailable.
        current_price = float(df['close'].iloc[-1])
        try:
            market_open, _, _ = is_market_open()
            if market_open:
                tick_prices = executor.get_current_price()
                if tick_prices:
                    bid, ask = tick_prices
                    current_price = float((bid + ask) / 2)
        except Exception:
            pass
        
        # S/R Zone Detection (informational only — zero trade impact)
        sr_brain_data = None
        self._last_sr_zones = []
        try:
            from support_resistance import detect_zones_triple, is_near_strong_zone
            from technical_analyzer import get_atr_value
            import MetaTrader5 as mt5
            import pandas as pd
            # Fetch dedicated H1 data for S/R (main df only has ANALYSIS_BARS=100)
            h1_rates_sr = mt5.copy_rates_from_pos(config.SYMBOL, mt5.TIMEFRAME_H1, 0, config.SR_LOOKBACK_H1 + 50)
            h4_rates = mt5.copy_rates_from_pos(config.SYMBOL, mt5.TIMEFRAME_H4, 0, config.SR_LOOKBACK_H4 + 50)
            d1_rates = mt5.copy_rates_from_pos(config.SYMBOL, mt5.TIMEFRAME_D1, 0, config.SR_LOOKBACK_D1 + 20)
            df_h1_sr = None
            if h1_rates_sr is not None and len(h1_rates_sr) > 0:
                df_h1_sr = pd.DataFrame(h1_rates_sr)
                df_h1_sr['datetime'] = pd.to_datetime(df_h1_sr['time'], unit='s')
            df_h4 = None
            if h4_rates is not None and len(h4_rates) > 0:
                df_h4 = pd.DataFrame(h4_rates)
                df_h4['datetime'] = pd.to_datetime(df_h4['time'], unit='s')
            df_d1 = None
            if d1_rates is not None and len(d1_rates) > 0:
                df_d1 = pd.DataFrame(d1_rates)
                df_d1['datetime'] = pd.to_datetime(df_d1['time'], unit='s')
            # Use dedicated H1 if available, fall back to main df
            df_h1_for_sr = df_h1_sr if df_h1_sr is not None else df
            if df_h4 is not None:
                sr_zones = detect_zones_triple(
                    df_h1_for_sr, df_h4, df_d1=df_d1,
                    merge_pips=config.SR_ZONE_MERGE_PIPS,
                    merge_pips_d1=config.SR_ZONE_MERGE_PIPS_D1,
                    max_age_bars=config.SR_ZONE_MAX_AGE_BARS,
                    min_touches=config.SR_MIN_TOUCHES,
                    lookback_h1=config.SR_LOOKBACK_H1,
                    lookback_h4=config.SR_LOOKBACK_H4,
                    lookback_d1=config.SR_LOOKBACK_D1,
                )
                atr_for_sr = get_atr_value(df)
                near_zone, zone_info = is_near_strong_zone(
                    sr_zones, current_price, atr_for_sr,
                    min_touches=config.SR_SCENARIO_MIN_TOUCHES,
                )
                zone_info_dict = None
                if zone_info is not None:
                    zone_info_dict = {
                        "midpoint": zone_info.midpoint,
                        "touches": zone_info.touches,
                        "zone_type": zone_info.zone_type,
                        "timeframe": zone_info.timeframe,
                        "price_low": zone_info.price_low,
                        "price_high": zone_info.price_high,
                        "confluence": zone_info.confluence,
                    }
                sr_brain_data = {
                    "confidence_adjustment": 0.0,
                    "confirmations": [],
                    "alerts": [],
                    "description": "",
                    "near_strong_zone": near_zone,
                    "near_zone_info": zone_info_dict,
                }
                self._last_sr_brain_data = sr_brain_data
                self._last_sr_zones = sr_zones
                zone_count = len(sr_zones)
                strong_count = sum(1 for z in sr_zones if z.touches >= 3)
                d1_count = sum(1 for z in sr_zones if z.timeframe == "D1")
                h4_count = sum(1 for z in sr_zones if z.timeframe == "H4")
                h1_count = sum(1 for z in sr_zones if z.timeframe == "H1")
                mtf_count = sum(1 for z in sr_zones if len(z.confluence) >= 2)
                log.info(f"   S/R: {zone_count} zones (D1:{d1_count} H4:{h4_count} H1:{h1_count} | {strong_count} strong, {mtf_count} MTF) | Near strong zone: {near_zone}")
                self._write_sr_zones_json(current_price)
                
                # Detect candlestick patterns with S/R proximity scaling
                from technical_analyzer import detect_candlestick_patterns
                self._last_candlestick_patterns = detect_candlestick_patterns(
                    df, sr_zones=sr_zones, current_price=current_price, atr=atr_for_sr
                )
                primary_pattern = self._last_candlestick_patterns.get("primary_pattern")
                if primary_pattern:
                    p_name = primary_pattern.get("name", "")
                    p_dir = primary_pattern.get("direction", "")
                    p_score = primary_pattern.get("final_score", 0)
                    p_mult = primary_pattern.get("sr_multiplier", 1.0)
                    log.info(f"   Pattern: {p_name} ({p_dir}) | score: {p_score:+.1f} (×{p_mult:.2f} S/R)")
        except Exception as e:
            log.debug(f"   S/R detection error (non-blocking): {e}")
        
        # Central Brain
        brain_result = analyze_with_brain(tech_data, ml_data, momentum_data, news_data, current_price, calendar_data=calendar_data, volatility_status=vol_status, m5_data=m5_status, sr_data=sr_brain_data)
        
        # Record snapshot in Cycle Memory
        from cycle_memory import CycleSnapshot
        snapshot = CycleSnapshot(
            timestamp=datetime.now(),
            score=brain_result.final_score,
            confidence=brain_result.confidence,
            decision=brain_result.decision,
            scenario=brain_result.scenario,
            tech_score=tech_data.get('score', 50),
            ml_score=ml_data.get('score', 50),
            momentum_score=momentum_data.get('score', 50),
            momentum_direction=brain_result.explanation.split('Direction: ')[-1].split('\n')[0] if 'Direction: ' in brain_result.explanation else 'neutral',
            momentum_strength=brain_result.explanation.split('Strength: ')[-1].split(' |')[0] if 'Strength: ' in brain_result.explanation else 'moderate',
            news_score=news_data.get('score', 50),
            current_price=current_price,
        )
        self.cycle_memory.add(snapshot)
        
        # GPT Confidence Validator
        cycle_history = self.cycle_memory.format_for_gpt()
        if getattr(config, 'USE_GPT_CONFIDENCE', False) and vol_status.get('status') != 'EXTREME':
            try:
                from gpt_confidence import validate_confidence
                gpt_result = validate_confidence(
                    brain_result, tech_data, ml_data, momentum_data,
                    news_data, calendar_data, vol_status, current_price,
                    cycle_history=cycle_history
                )
                
                if gpt_result["action"] == "BOOST" and gpt_result["adjustment"] > 0:
                    brain_result.confidence = min(100, brain_result.confidence + gpt_result["adjustment"])
                elif gpt_result["action"] == "REDUCE" and gpt_result["adjustment"] > 0:
                    brain_result.confidence = max(0, brain_result.confidence - gpt_result["adjustment"])
                
                # Re-classificar confidence_level
                if brain_result.confidence >= 80:
                    brain_result.confidence_level = "VERY_HIGH"
                elif brain_result.confidence >= 65:
                    brain_result.confidence_level = "HIGH"
                elif brain_result.confidence >= 50:
                    brain_result.confidence_level = "MEDIUM"
                elif brain_result.confidence >= 35:
                    brain_result.confidence_level = "LOW"
                else:
                    brain_result.confidence_level = "VERY_LOW"
                
                brain_result.gpt_validation = gpt_result
                
                # Increment stats
                self.gpt_stats[gpt_result["action"].lower()] += 1
                if gpt_result.get("from_cache"):
                    self.gpt_stats["from_cache"] += 1
                
                cache_tag = " (cache)" if gpt_result.get("from_cache") else ""
                if gpt_result["action"] != "CONFIRM" and gpt_result["adjustment"] > 0:
                    sign = "+" if gpt_result["action"] == "BOOST" else "-"
                    log.info(f"   🤖 GPT: {gpt_result['action']} ({sign}{gpt_result['adjustment']}) — {gpt_result['reason']}{cache_tag}")
                else:
                    log.info(f"   🤖 GPT: CONFIRM — {gpt_result['reason']}{cache_tag}")
                
            except Exception as e:
                log.warning(f"GPT Confidence error (fallback CONFIRM): {e}")
                self.gpt_stats["confirm"] += 1
        
        # Detailed log
        log.info(f"   🧠 Scenario: {brain_result.scenario_description}")
        log.info(f"   🧠 Score: {brain_result.final_score:.1f} | Confidence: {brain_result.confidence:.1f} ({brain_result.confidence_level})")
        log.info(f"   🧠 Decision: {brain_result.decision}")

        try:
            if brain_result.scenario == "ml_vs_tech_conflito":
                tech_s = float(tech_data.get('score', 50.0))
                ml_s = float(ml_data.get('score', 50.0))
                score = float(brain_result.final_score)
                override_used = (58.0 <= score < 65.0) and (brain_result.decision in ("BUY", "STRONG_BUY"))
                used_txt = "YES" if override_used else "NO"
                log.info(
                    f"   Scenario: ml_vs_tech_conflito (Tech={tech_s:.1f} vs ML={ml_s:.1f}) | "
                    f"BUY threshold overridden: 58 | override_used={used_txt} | score={score:.1f}"
                )
        except Exception:
            pass
        
        # Full explanation log in DEBUG
        for line in brain_result.explanation.split('\n'):
            log.debug(f"   {line}")
        
        # Build concise summary for Discord
        summary_lines = [
            f"Scenario: {brain_result.scenario_description}",
        ]
        # Top confirmations (max 3)
        for conf in brain_result.confirmations[:3]:
            summary_lines.append(f"• {conf}")
        # Top alerts (max 2)
        for alert in brain_result.alerts[:2]:
            summary_lines.append(f"⚠ {alert}")
        summary_lines.append(f"Confidence: {brain_result.confidence_level} ({brain_result.confidence:.0f}/100)")
        # GPT Validator info
        if brain_result.gpt_validation and brain_result.gpt_validation.get("action"):
            gpt = brain_result.gpt_validation
            if gpt["action"] != "CONFIRM" and gpt["adjustment"] > 0:
                sign = "+" if gpt["action"] == "BOOST" else "-"
                summary_lines.append(f"🤖 GPT: {gpt['action']} ({sign}{gpt['adjustment']}) — {gpt.get('reason', '')}")
            else:
                summary_lines.append(f"🤖 GPT: CONFIRM")
        brain_summary = "\n".join(summary_lines)
        
        # Store data for heartbeat
        self._last_calendar_data = calendar_data
        self._last_vol_status = vol_status
        self._last_current_price = current_price
        self._last_scenario_description = brain_result.scenario_description
        self._last_gpt_validation = brain_result.gpt_validation
        
        # Check minimum confidence
        direction = None
        hold_forced = False
        original_decision = None
        hold_reason = None
        if is_actionable_signal(brain_result.decision):
            if brain_result.confidence >= config.BRAIN_MIN_CONFIDENCE:
                direction = get_trade_direction(brain_result.decision)
            else:
                hold_forced = True
                original_decision = brain_result.decision
                hold_reason = f"confidence {brain_result.confidence:.1f}% < {config.BRAIN_MIN_CONFIDENCE}%"
                log.info(f"   ⚠️ Confidence ({brain_result.confidence:.1f}) below minimum ({config.BRAIN_MIN_CONFIDENCE}) - forced HOLD")
        
        # Persist last_analysis for dashboard
        try:
            # Build intel_feed from existing cache (no extra requests)
            intel_feed = self._build_intel_feed(
                news_data, calendar_data, brain_result, get_hybrid_score_cached
            )

            # Build rich scalar fields for history DB (no trade impact)
            utc_hour = None
            session_name = None
            try:
                from agent_data_builder import get_session_name
                utc_hour = datetime.utcnow().hour
                session_name = get_session_name(utc_hour)
            except Exception:
                pass

            indicators = {}
            try:
                # Prefer detailed dicts (tech_data/momentum_data) when available
                rsi_block = (tech_data or {}).get("rsi", {}) if isinstance(tech_data, dict) else {}
                macd_block = (tech_data or {}).get("macd", {}) if isinstance(tech_data, dict) else {}
                ema_block = (tech_data or {}).get("ema", {}) if isinstance(tech_data, dict) else {}
                bb_block = (tech_data or {}).get("bollinger", {}) if isinstance(tech_data, dict) else {}

                indicators["rsi_14"] = rsi_block.get("value")
                indicators["macd"] = macd_block.get("macd")
                indicators["macd_signal"] = macd_block.get("signal")
                indicators["macd_hist"] = macd_block.get("histogram")
                indicators["ema_9"] = ema_block.get("ema9")
                indicators["ema_21"] = ema_block.get("ema21")
                indicators["ema_50"] = ema_block.get("ema50")
                indicators["bb_upper"] = bb_block.get("upper")
                indicators["bb_middle"] = bb_block.get("middle")
                indicators["bb_lower"] = bb_block.get("lower")
                indicators["bb_position"] = bb_block.get("position")

                # Fallback to dataframe last row columns when missing
                last_row = None
                try:
                    last_row = df.iloc[-1] if df is not None and len(df) > 0 else None
                except Exception:
                    last_row = None

                def _df_get(col: str):
                    try:
                        if last_row is not None and col in last_row:
                            v = last_row[col]
                            return float(v) if v is not None else None
                    except Exception:
                        return None
                    return None

                if indicators.get("rsi_14") is None:
                    indicators["rsi_14"] = _df_get("rsi_14")
                if indicators.get("macd") is None:
                    indicators["macd"] = _df_get("macd")
                if indicators.get("macd_signal") is None:
                    indicators["macd_signal"] = _df_get("macd_signal")
                if indicators.get("macd_hist") is None:
                    indicators["macd_hist"] = _df_get("macd_hist")
                if indicators.get("ema_9") is None:
                    indicators["ema_9"] = _df_get("ema_9")
                if indicators.get("ema_21") is None:
                    indicators["ema_21"] = _df_get("ema_21")
                if indicators.get("ema_50") is None:
                    indicators["ema_50"] = _df_get("ema_50")
                if indicators.get("bb_upper") is None:
                    indicators["bb_upper"] = _df_get("bb_upper")
                if indicators.get("bb_middle") is None:
                    indicators["bb_middle"] = _df_get("bb_middle")
                if indicators.get("bb_lower") is None:
                    indicators["bb_lower"] = _df_get("bb_lower")
                if indicators.get("bb_position") is None:
                    indicators["bb_position"] = _df_get("bb_position")

                # price_vs_ema50_pct
                if indicators.get("ema_50") is not None and current_price:
                    try:
                        indicators["price_vs_ema50_pct"] = ((float(current_price) - float(indicators["ema_50"])) / float(current_price)) * 100
                    except Exception:
                        indicators["price_vs_ema50_pct"] = None
            except Exception:
                indicators = {}

            try:
                adx_block = (momentum_data or {}).get("adx", {}) if isinstance(momentum_data, dict) else {}
                atr_block = (momentum_data or {}).get("atr", {}) if isinstance(momentum_data, dict) else {}
                vol_block = (momentum_data or {}).get("volume", {}) if isinstance(momentum_data, dict) else {}
                consec_block = (momentum_data or {}).get("consecutive", {}) if isinstance(momentum_data, dict) else {}
                breakout_block = (momentum_data or {}).get("breakout", {}) if isinstance(momentum_data, dict) else {}

                indicators["adx_14"] = adx_block.get("adx_value")
                indicators["plus_di"] = adx_block.get("plus_di")
                indicators["minus_di"] = adx_block.get("minus_di")
                indicators["atr_14"] = atr_block.get("atr_value") if "atr_value" in atr_block else atr_block.get("atr_current")
                indicators["volume_ratio"] = vol_block.get("volume_ratio")
                indicators["volume_classification"] = vol_block.get("volume_classification")
                indicators["momentum_direction"] = (momentum_data or {}).get("direction")
                indicators["consecutive_count"] = consec_block.get("consecutive_count")
                indicators["consecutive_direction"] = consec_block.get("consecutive_direction")
                indicators["breakout_detected"] = breakout_block.get("breakout_detected")
                indicators["breakout_type"] = breakout_block.get("breakout_type")
            except Exception:
                pass

            ml_meta = {}
            try:
                score_h1 = float(ml_data.get("score_h1", ml_data.get("score", 50.0)))
                score_h4 = float(ml_data.get("score_h4", ml_data.get("score", 50.0)))
                ml_meta = {
                    "h1_prob": score_h1 / 100.0,
                    "h4_prob": score_h4 / 100.0,
                    "direction": ml_data.get("prediction"),
                }
            except Exception:
                ml_meta = {}

            prev_last_analysis = self.last_analysis if isinstance(self.last_analysis, dict) else {}
            preserved_proactive = prev_last_analysis.get("proactive_analysis")
            preserved_agent = prev_last_analysis.get("agent_decision")

            self.last_analysis = {
                "timestamp": datetime.now().isoformat(),
                "decision": "HOLD" if hold_forced else brain_result.decision,
                "final_score": brain_result.final_score,
                "confidence": brain_result.confidence,
                "confidence_level": brain_result.confidence_level,
                "scenario": brain_result.scenario,
                "scenario_description": brain_result.scenario_description,
                "tech_score": float(tech_data.get("score", 50.0)),
                "news_score": float(news_data.get("score", 50.0)),
                "ml_score": float(ml_data.get("score", 50.0)),
                "momentum_score": float(momentum_data.get("score", 50.0)),
                "calendar_score": float(calendar_data.get("score", 50.0)) if calendar_data else 50.0,
                "current_price": float(current_price),
                "volatility_status": vol_status.get("status", "NORMAL") if vol_status else "NORMAL",
                "volatility_description": vol_status.get("description", "") if vol_status else "",
                "gpt_validation": brain_result.gpt_validation,
                "intel_feed": intel_feed,
                "hold_forced": hold_forced,
                "original_decision": original_decision,
                "hold_reason": hold_reason,
                "mtf_trend": brain_result.mtf_trend,
                "volume_gate": brain_result.volume_gate,
                "utc_hour": utc_hour,
                "session_name": session_name,
                "indicators": indicators,
                "ml": ml_meta,
            }

            # Preserve nested fields that are not recalculated every analysis cycle
            if preserved_proactive and "proactive_analysis" not in self.last_analysis:
                self.last_analysis["proactive_analysis"] = preserved_proactive
            if preserved_agent and "agent_decision" not in self.last_analysis:
                self.last_analysis["agent_decision"] = preserved_agent
        except Exception as e:
            log.warning(f"Rich data enrichment failed: {e}")

        record_analysis(self.last_analysis)
        
        # Return decision tuple + agent_data dict for deferred Agent call
        # Agent call moved to _analysis_cycle() AFTER safety checks pass
        agent_data = {
            "brain_result": brain_result,
            "tech_data": tech_data,
            "ml_data": ml_data,
            "momentum_data": momentum_data,
            "news_data": news_data,
            "calendar_data": calendar_data,
            "current_price": current_price,
            "vol_status": vol_status,
            "df": df,
            "hold_forced": hold_forced,
            "original_decision": original_decision,
            "hold_reason": hold_reason,
        }
        
        return (
            brain_result.decision,
            brain_result.final_score,
            brain_result.confidence,
            direction,
            tech_data['score'],
            news_data['score'],
            ml_data['score'],
            brain_summary,
            agent_data,
        )
    
    def _confluence_analysis(self, df):
        """
        Analysis via confluence.py (fallback).
        
        Returns:
            Tuple: (decision, final_score, confidence, direction, tech_score, news_score, ml_score, explanation)
            direction is None if not actionable
        """
        log.info("   📊 Mode: CONFLUENCE (fallback)")
        
        # Technical Score
        tech_score, tech_breakdown = calculate_technical_score(df)
        log.info(f"   Technical: {tech_score:.1f}/100")
        
        # News Score (with cache)
        news_score, news_data = news_cache.get_score()
        log.info(f"   News: {news_score:.1f}/100")
        
        # Score ML
        try:
            from ml_predictor import get_ml_score
            ml_score, ml_prob = get_ml_score(df)
        except:
            ml_score, ml_prob = 50.0, 0.5
        log.info(f"   ML: {ml_score:.1f}/100 (prob: {ml_prob:.3f})")
        
        # Confluence
        result = analyze_confluence(tech_score, news_score, ml_score, ml_prob)
        
        direction = None
        if confluence_is_actionable(result.decision):
            direction = confluence_get_direction(result.decision)
        
        # Persist last_analysis for dashboard (modo confluence)
        try:
            current_price = float(df['close'].iloc[-1])
            try:
                market_open, _, _ = is_market_open()
                if market_open:
                    tick_prices = executor.get_current_price()
                    if tick_prices:
                        bid, ask = tick_prices
                        current_price = float((bid + ask) / 2)
            except Exception:
                pass
        except Exception:
            current_price = 0.0
        try:
            prev_last_analysis = self.last_analysis if isinstance(self.last_analysis, dict) else {}
            preserved_proactive = prev_last_analysis.get("proactive_analysis")
            preserved_agent = prev_last_analysis.get("agent_decision")

            self.last_analysis = {
                "timestamp": datetime.now().isoformat(),
                "decision": result.decision,
                "final_score": result.final_score,
                "confidence": result.confidence,
                "confidence_level": "N/A",
                "scenario": "confluence",
                "scenario_description": "Confluence (fallback)",
                "tech_score": float(tech_score),
                "news_score": float(news_score),
                "ml_score": float(ml_score),
                "momentum_score": 50.0,
                "calendar_score": 50.0,
                "current_price": current_price,
                "volatility_status": "NORMAL",
                "volatility_description": "",
                "gpt_validation": None,
            }

            # Preserve nested fields that are not recalculated every analysis cycle
            if preserved_proactive and "proactive_analysis" not in self.last_analysis:
                self.last_analysis["proactive_analysis"] = preserved_proactive
            if preserved_agent and "agent_decision" not in self.last_analysis:
                self.last_analysis["agent_decision"] = preserved_agent
        except Exception:
            pass
        
        record_analysis(self.last_analysis)
        
        # Confluence mode doesn't use Agent, return None for agent_data
        return (
            result.decision,
            result.final_score,
            result.confidence,
            direction,
            tech_score,
            news_score,
            ml_score,
            "",
            None,  # agent_data - not used in confluence mode
        )
    
    def _call_agent_shadow_mode(
        self,
        brain_result,
        tech_data,
        ml_data,
        momentum_data,
        news_data,
        calendar_data,
        current_price,
        vol_status,
        df,
        hold_forced: bool = False,
        original_decision: str = None,
        hold_reason: str = None,
    ):
        """
        Call AI Agent in shadow mode.
        Agent decides but Brain executes. Both decisions logged for comparison.
        
        When hold_forced=True, the Brain wanted to trade but confidence was too low.
        The Agent receives the original signal and can decide to AGREE_HOLD or OVERRIDE_OPEN.
        """
        import asyncio
        from ai_agent import get_agent, AgentDecision
        from agent_data_builder import build_data_package, get_session_name
        
        agent = get_agent()
        if not agent.is_enabled():
            return
        
        trigger_type = "HOLD_FORCED" if hold_forced else "SIGNAL"
        log.info(f"   🤖 Calling AI Agent (shadow mode, trigger={trigger_type})...")
        
        # Build session context
        session_context = {
            "session_name": get_session_name(datetime.utcnow().hour),
            "hour_utc": datetime.utcnow().hour,
            "today_trades": self.daily_stats.get("trades", 0),
            "today_wins": self.daily_stats.get("wins", 0),
            "today_losses": self.daily_stats.get("losses", 0),
            "today_pnl": self.daily_stats.get("pnl", 0),
            "last_5_results": [],  # Could be populated from closed_trades_today
            "consecutive_losses": 0,
            "hold_forced": {
                "is_forced": hold_forced,
                "original_decision": original_decision,
                "reason": hold_reason,
            } if hold_forced else None,
        }
        
        # Get candle data
        h1_candles = []
        m5_candles = []
        d1_candles = []
        h4_candles = []
        try:
            import MetaTrader5 as mt5
            
            # H1 candles from df
            for i in range(max(0, len(df) - 20), len(df)):
                row = df.iloc[i]
                h1_candles.append({
                    "time": str(row.get("datetime", "")),
                    "open": float(row["open"]),
                    "high": float(row["high"]),
                    "low": float(row["low"]),
                    "close": float(row["close"]),
                    "tick_volume": int(row.get("tick_volume", 0)),
                })
            
            # M5 candles
            m5_rates = mt5.copy_rates_from_pos(config.SYMBOL, mt5.TIMEFRAME_M5, 0, 10)
            if m5_rates is not None:
                for r in m5_rates:
                    m5_candles.append({
                        "time": datetime.fromtimestamp(r["time"]).isoformat(),
                        "open": float(r["open"]),
                        "high": float(r["high"]),
                        "low": float(r["low"]),
                        "close": float(r["close"]),
                        "tick_volume": int(r["tick_volume"]),
                    })
            
            # D1 candles (weekly context)
            d1_rates = mt5.copy_rates_from_pos(config.SYMBOL, mt5.TIMEFRAME_D1, 0, 10)
            if d1_rates is not None:
                for r in d1_rates:
                    d1_candles.append({
                        "time": datetime.fromtimestamp(r["time"]).isoformat(),
                        "open": float(r["open"]),
                        "high": float(r["high"]),
                        "low": float(r["low"]),
                        "close": float(r["close"]),
                        "tick_volume": int(r["tick_volume"]),
                    })
            
            # H4 candles (2-3 day structure)
            h4_rates = mt5.copy_rates_from_pos(config.SYMBOL, mt5.TIMEFRAME_H4, 0, 20)
            if h4_rates is not None:
                for r in h4_rates:
                    h4_candles.append({
                        "time": datetime.fromtimestamp(r["time"]).isoformat(),
                        "open": float(r["open"]),
                        "high": float(r["high"]),
                        "low": float(r["low"]),
                        "close": float(r["close"]),
                        "tick_volume": int(r["tick_volume"]),
                    })
        except Exception as e:
            log.debug(f"Error getting candle data for Agent: {e}")
        
        # Get current price data
        price_data = {"bid": current_price, "ask": current_price, "spread": 0}
        try:
            tick = executor.get_current_price()
            if tick:
                price_data = {"bid": tick[0], "ask": tick[1], "spread": (tick[1] - tick[0]) / 0.1}
        except Exception:
            pass
        
        # Get open positions
        positions = []
        try:
            if self.executes_trades:
                pos_list = executor.get_open_positions()
                for p in pos_list[:3]:
                    positions.append({
                        "ticket": p.ticket,
                        "type": "BUY" if p.type == 0 else "SELL",
                        "price_open": p.price_open,
                        "price_current": p.price_current,
                        "profit": p.profit,
                        "sl": p.sl,
                        "tp": p.tp,
                    })
        except Exception:
            pass
        
        # Build data package
        # Get S/R zones and candlestick patterns from instance state
        sr_zones_for_agent = getattr(self, '_last_sr_zones', []) or []
        candlestick_patterns_for_agent = getattr(self, '_last_candlestick_patterns', None)
        
        # Build S/R proximity data
        sr_proximity_data = None
        try:
            sr_brain_data = getattr(self, '_last_sr_brain_data', None)
            if sr_brain_data:
                sr_proximity_data = {
                    "near_strong_zone": sr_brain_data.get("near_strong_zone", False),
                    "near_zone_info": sr_brain_data.get("near_zone_info"),
                    "dist_to_nearest_pips": sr_brain_data.get("dist_to_nearest_pips"),
                }
        except Exception:
            pass
        
        # ================================================================
        # NEW DATA: Agent Memory (last 3-5 decisions)
        # ================================================================
        agent_memory = []
        try:
            agent_memory = get_recent_agent_decisions(5)
        except Exception as e:
            log.debug(f"Error getting agent memory: {e}")
        
        # ================================================================
        # NEW DATA: Trade Feedback (recent trades with Agent accuracy)
        # ================================================================
        trade_feedback = None
        try:
            trade_feedback = get_trade_feedback(5)
        except Exception as e:
            log.debug(f"Error getting trade feedback: {e}")
        
        # ================================================================
        # NEW DATA: Delta Context (what changed since last cycle)
        # ================================================================
        delta_context = None
        try:
            prev = getattr(self, '_prev_agent_cycle_data', None)
            current_rsi = tech_data.get("rsi", {}).get("value", 50)
            current_volume_ratio = momentum_data.get("volume", {}).get("volume_ratio", 1.0)
            
            if prev:
                price_change_pips = (current_price - prev.get("price", current_price)) / 0.1
                rsi_change = current_rsi - prev.get("rsi", current_rsi)
                prev_vol = prev.get("volume_ratio", 1.0) or 1.0
                volume_change_pct = ((current_volume_ratio - prev_vol) / prev_vol) * 100 if prev_vol else 0
                
                # Detect significant events
                significant_events = []
                if abs(price_change_pips) > 50:
                    direction = "up" if price_change_pips > 0 else "down"
                    significant_events.append(f"Price moved {abs(price_change_pips):.0f} pips {direction}")
                if abs(rsi_change) > 10:
                    direction = "rose" if rsi_change > 0 else "fell"
                    significant_events.append(f"RSI {direction} {abs(rsi_change):.1f} points")
                if abs(volume_change_pct) > 50:
                    direction = "spiked" if volume_change_pct > 0 else "dropped"
                    significant_events.append(f"Volume {direction} {abs(volume_change_pct):.0f}%")
                
                delta_context = {
                    "price_change_pips": price_change_pips,
                    "rsi_change": rsi_change,
                    "volume_change_pct": volume_change_pct,
                    "significant_events": significant_events,
                }
            
            # Store current cycle data for next comparison
            self._prev_agent_cycle_data = {
                "price": current_price,
                "rsi": current_rsi,
                "volume_ratio": current_volume_ratio,
            }
        except Exception as e:
            log.debug(f"Error building delta context: {e}")
        
        # ================================================================
        # NEW DATA: Portfolio Awareness
        # ================================================================
        portfolio_data = None
        try:
            daily_pnl = self.daily_stats.get("pnl", 0)
            daily_wins = self.daily_stats.get("wins", 0)
            daily_losses = self.daily_stats.get("losses", 0)
            total_trades = daily_wins + daily_losses
            win_rate = (daily_wins / total_trades * 100) if total_trades > 0 else 0
            
            # Calculate drawdown (simple: from daily high)
            account_balance = get_account_balance() if self.executes_trades else config.CAPITAL_INICIAL
            drawdown_pct = 0
            if daily_pnl < 0 and account_balance > 0:
                drawdown_pct = abs(daily_pnl) / account_balance * 100
            
            # Risk budget: assume 2% daily max risk, calculate remaining
            max_daily_risk = account_balance * 0.02
            used_risk = abs(min(daily_pnl, 0))  # Only count losses
            risk_remaining = max(0, max_daily_risk - used_risk)
            risk_budget_remaining_pct = (risk_remaining / max_daily_risk * 100) if max_daily_risk > 0 else 100
            
            portfolio_data = {
                "daily_pnl": daily_pnl,
                "daily_wins": daily_wins,
                "daily_losses": daily_losses,
                "win_rate_today": win_rate,
                "drawdown_pct": drawdown_pct,
                "risk_budget_remaining_pct": risk_budget_remaining_pct,
            }
        except Exception as e:
            log.debug(f"Error building portfolio data: {e}")
        
        # ================================================================
        # NEW DATA: Regime Context (trending/ranging, ADX/ATR analysis)
        # ================================================================
        regime_context = None
        try:
            adx_value = momentum_data.get("adx", {}).get("adx_value", 0)
            atr_value = momentum_data.get("atr", {}).get("atr_value", 0)
            
            # Determine regime
            if adx_value >= 25:
                regime = "trending"
                if adx_value >= 40:
                    trend_strength = "strong"
                else:
                    trend_strength = "moderate"
            else:
                regime = "ranging"
                trend_strength = "weak"
            
            # Count hours ADX above 25 (from H1 data if available)
            adx_hours_above_25 = 0
            try:
                # Use stored ADX history if available
                adx_history = getattr(self, '_adx_history', [])
                adx_history.append(adx_value)
                adx_history = adx_history[-24:]  # Keep last 24 hours
                self._adx_history = adx_history
                adx_hours_above_25 = sum(1 for v in adx_history if v >= 25)
            except Exception:
                pass
            
            # ATR vs weekly average (estimate from current ATR)
            atr_vs_weekly = 1.0
            try:
                atr_history = getattr(self, '_atr_history', [])
                atr_history.append(atr_value)
                atr_history = atr_history[-120:]  # ~5 days of hourly data
                self._atr_history = atr_history
                if len(atr_history) > 20:
                    weekly_avg = sum(atr_history) / len(atr_history)
                    if weekly_avg > 0:
                        atr_vs_weekly = atr_value / weekly_avg
            except Exception:
                pass
            
            regime_context = {
                "regime": regime,
                "adx_hours_above_25": adx_hours_above_25,
                "atr_vs_weekly_avg": atr_vs_weekly,
                "trend_strength": trend_strength,
            }
        except Exception as e:
            log.debug(f"Error building regime context: {e}")
        
        data_package = build_data_package(
            brain_result=brain_result,
            tech_data=tech_data,
            ml_data=ml_data,
            momentum_data=momentum_data,
            news_data=news_data,
            calendar_data=calendar_data,
            h1_candles=h1_candles,
            m5_candles=m5_candles,
            current_price=price_data,
            positions=positions,
            session_context=session_context,
            volatility_status=vol_status or {},
            sr_zones=sr_zones_for_agent,
            candlestick_patterns=candlestick_patterns_for_agent,
            sr_proximity=sr_proximity_data,
            d1_candles=d1_candles,
            h4_candles=h4_candles,
            agent_memory=agent_memory,
            trade_feedback=trade_feedback,
            delta_context=delta_context,
            portfolio=portfolio_data,
            regime_context=regime_context,
        )

        try:
            d1_first = d1_candles[0].get("time") if d1_candles else None
            d1_last = d1_candles[-1].get("time") if d1_candles else None
            h4_first = h4_candles[0].get("time") if h4_candles else None
            h4_last = h4_candles[-1].get("time") if h4_candles else None

            ns = data_package.get("nearest_support")
            nr = data_package.get("nearest_resistance")
            log.info(
                f"   🤖 Agent package: h4_count={len(data_package.get('h4_candles', []))} | "
                f"nearest_support={ns} | nearest_resistance={nr} | "
                f"d1_time={d1_first}->{d1_last} | h4_time={h4_first}->{h4_last}"
            )
        except Exception as e:
            log.debug(f"Agent package monitoring log failed (non-blocking): {e}")
        
        # Call Agent (async) - use agent_decide() wrapper for memory injection/saving
        from ai_agent import agent_decide
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            agent_result = loop.run_until_complete(agent_decide(data_package))
            loop.close()
        except Exception as e:
            log.warning(f"Agent call failed: {e}")
            return
        
        # Log result
        agent_decision = agent_result.decision
        agent_confidence = agent_result.confidence
        
        # Determine agreement based on trigger type
        if hold_forced:
            # HOLD FORCED: Brain blocked a trade due to low confidence
            # Agent agrees if it also wants to hold, disagrees if it wants to override
            if agent_decision in ("AGREE_HOLD", "WAIT", "HOLD", "REJECT"):
                agreement = True  # Agent agrees with blocking the trade
            elif agent_decision in ("OVERRIDE_OPEN", "BUY", "SELL", "STRONG_BUY", "STRONG_SELL"):
                agreement = False  # Agent wants to open despite Brain's block
            else:
                agreement = True  # Default to agreement for unknown responses
            brain_display = f"HOLD_FORCED ({original_decision})"
        else:
            # Normal signal: Brain wants to trade
            brain_dir = "BUY" if "BUY" in brain_result.decision else ("SELL" if "SELL" in brain_result.decision else "HOLD")
            agent_dir = "BUY" if "BUY" in agent_decision else ("SELL" if "SELL" in agent_decision else "HOLD")
            
            # REJECT/WAIT = disagreement with BUY/SELL
            if agent_decision in ("REJECT", "WAIT"):
                agreement = False
            else:
                agreement = (brain_dir == agent_dir)
            brain_display = brain_result.decision
        
        agreement_str = "✅ AGREE" if agreement else "❌ DISAGREE"
        log.info(f"   🤖 Agent: {agent_decision} (conf={agent_confidence}) | {agreement_str}")
        if agent_result.reasoning:
            # Truncate for log
            reasoning_short = agent_result.reasoning[:150] + "..." if len(agent_result.reasoning) > 150 else agent_result.reasoning
            log.info(f"   🤖 Reasoning: {reasoning_short}")
        
        # In shadow mode, Brain executes
        executed = "BRAIN"
        mode = agent.get_mode()
        
        # Record to SQLite (use brain_display for HOLD_FORCED visibility)
        record_agent_decision(
            brain_decision=brain_display,
            brain_score=brain_result.final_score,
            brain_confidence=brain_result.confidence,
            agent_result=agent_result.to_dict(),
            executed=executed,
            agreement=agreement,
        )
        
        # Send Discord alert
        alert_agent_decision(
            brain_decision=brain_display,
            brain_score=brain_result.final_score,
            brain_confidence=brain_result.confidence,
            agent_decision=agent_decision,
            agent_confidence=agent_confidence,
            agent_reasoning=agent_result.reasoning,
            agent_key_factors=agent_result.key_factors,
            agent_concerns=agent_result.concerns,
            agreement=agreement,
            executed=executed,
            mode=mode,
            latency_ms=agent_result.latency_ms,
            tokens_used=agent_result.input_tokens + agent_result.output_tokens,
        )
        
        # Store in last_analysis for dashboard
        if self.last_analysis:
            self.last_analysis["agent_decision"] = {
                "decision": agent_decision,
                "confidence": agent_confidence,
                "reasoning": agent_result.reasoning,
                "key_factors": agent_result.key_factors,
                "concerns": agent_result.concerns,
                "agreement": agreement,
                "executed": executed,
                "latency_ms": agent_result.latency_ms,
                "trigger_type": "HOLD_FORCED" if hold_forced else "SIGNAL",
                "original_decision": original_decision if hold_forced else None,
            }
    
    def _check_heartbeat(self) -> None:
        """Send periodic status heartbeat to Discord (keep-alive)."""
        now = datetime.now()
        if self.last_heartbeat and (now - self.last_heartbeat) < timedelta(minutes=config.HEARTBEAT_INTERVAL_MINUTES):
            return

        open_positions = 0
        if self.executes_trades:
            try:
                open_positions = len(get_positions())
            except Exception:
                open_positions = 0

        last_analysis_time = "N/A"
        try:
            if self.last_analysis and self.last_analysis.get("timestamp"):
                last_analysis_time = self.last_analysis["timestamp"]
        except Exception:
            pass

        uptime = "N/A"
        if self.session_start_time:
            delta = now - self.session_start_time
            hours, remainder = divmod(int(delta.total_seconds()), 3600)
            minutes = remainder // 60
            uptime = f"{hours}h {minutes}m"

        alert_heartbeat_full(
            bot_name=config.DISCORD_BOT_NAME,
            uptime=uptime,
            open_positions=open_positions,
            last_analysis_time=last_analysis_time,
        )
        log.info("   � Heartbeat sent to Discord")
        self.last_heartbeat = now
    
    def _get_dominant_pillar(self, tech_score, news_score, ml_score):
        """Identify the pillar that contributes most to the HOLD decision (furthest from 50)."""
        pillars = {
            "Technical": tech_score,
            "News": news_score,
            "ML": ml_score,
        }
        
        # The most extreme pillar (furthest from 50) is the one that "pulls" the most
        dominant_name = max(pillars, key=lambda k: abs(pillars[k] - 50))
        dominant_score = pillars[dominant_name]
        
        if dominant_score < 50:
            direction = "bearish"
        elif dominant_score > 50:
            direction = "bullish"
        else:
            direction = "neutral"
        
        return f"{dominant_name}: {dominant_score:.0f}/100 ({direction})"
    
    @staticmethod
    def _safe_str(s):
        """Sanitize string to remove surrogates that break UTF-8 encoding."""
        if not isinstance(s, str):
            return s
        return s.encode('utf-8', errors='surrogatepass').decode('utf-8', errors='replace')

    def _write_sr_zones_json(self, current_price: float):
        """Write nearest S/R zones to JSON for MQL5 EA to draw on chart."""
        try:
            import json
            sr_zones_raw = getattr(self, '_last_sr_zones', []) or []
            cp = current_price
            if not sr_zones_raw or not cp:
                log.debug(f"   S/R JSON: skipped (zones={len(sr_zones_raw)}, price={cp})")
                return

            above = sorted([z for z in sr_zones_raw if z.midpoint > cp], key=lambda z: z.midpoint)[:4]
            below = sorted([z for z in sr_zones_raw if z.midpoint <= cp], key=lambda z: -z.midpoint)[:4]

            zones_out = []
            for z in above + below:
                zones_out.append({
                    "price": round(z.midpoint, 2),
                    "zone_type": z.zone_type,
                    "touches": z.touches,
                    "timeframe": z.timeframe,
                    "confluence": z.confluence if z.confluence else [],
                    "strength": z.strength,
                    "position": "above" if z.midpoint > cp else "below",
                })

            payload = {
                "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "current_price": round(cp, 2),
                "zones_count": len(zones_out),
                "zones": zones_out,
            }

            json_path = getattr(config, 'SR_ZONES_JSON_PATH', None)
            if json_path:
                with open(json_path, 'w', encoding='utf-8') as f:
                    json.dump(payload, f, indent=2)
                log.info(f"   S/R JSON: wrote {len(zones_out)} zones to MQL5\\Files\\sr_zones.json")
        except Exception as e:
            log.warning(f"   S/R JSON write error (non-blocking): {e}")

    def _build_intel_feed(self, news_data, calendar_data, brain_result, get_hybrid_score_cached_fn):
        """Build intel_feed dict for dashboard from existing cached data (zero extra requests)."""
        try:
            _s = self._safe_str
            # Headlines + macro from hybrid cache
            cached = get_hybrid_score_cached_fn()
            result = cached.get("result", {})
            components = result.get("components", {})

            # Top 8 headlines (most recent / highest weight first — already sorted)
            raw_headlines = components.get("headlines", {}).get("details", [])
            headlines = [
                {
                    "title": _s(h.get("title", "")),
                    "score": h.get("score", 50),
                    "method": h.get("method", "keywords"),
                    "age_hours": h.get("age_hours", 0),
                    "source": _s(h.get("source", "")),
                    "category": h.get("category", "gold"),
                }
                for h in raw_headlines[:8]
            ]

            # Macro components
            dxy_comp = components.get("dxy", {})
            yields_comp = components.get("yields", {})
            vix_comp = components.get("vix", {})

            macro = {
                "dxy": {
                    "value": dxy_comp.get("current"),
                    "change_pct": dxy_comp.get("change_percent"),
                    "score": dxy_comp.get("score", 50),
                },
                "yields": {
                    "value": yields_comp.get("current"),
                    "change_pct": yields_comp.get("change_percent"),
                    "score": yields_comp.get("score", 50),
                },
                "vix": {
                    "value": vix_comp.get("current"),
                    "change_pct": vix_comp.get("change_percent"),
                    "score": vix_comp.get("score", 50),
                    "is_extreme": vix_comp.get("is_extreme", False),
                },
            }

            # Cache age
            cache_age_minutes = cached.get("cache_age_minutes", 0)

            # Analysis method
            analysis_method = components.get("headlines", {}).get("analysis_method", "keywords")
            # The method is per-headline; get the dominant one
            if headlines:
                gpt_count = sum(1 for h in headlines if h.get("method") == "gpt")
                analysis_method = "gpt" if gpt_count > len(headlines) / 2 else "keywords"

            # Calendar details
            cal = calendar_data or {}
            closest_event = cal.get("closest_event")
            
            # Upcoming events (HIGH + MEDIUM for dashboard visibility)
            try:
                from economic_calendar import get_upcoming_events
                upcoming = get_upcoming_events(max_events=5)
                for evt in upcoming:
                    if isinstance(evt.get("name"), str):
                        evt["name"] = _s(evt["name"])
            except Exception:
                upcoming = []
            
            # Override phase_description when NORMAL but upcoming events exist
            phase_desc = cal.get("phase_description", "")
            if cal.get("phase") == "normal" and upcoming:
                n_high = sum(1 for e in upcoming if e.get("importance") == "HIGH")
                n_med = sum(1 for e in upcoming if e.get("importance") == "MEDIUM")
                parts = []
                if n_high: parts.append(f"{n_high} HIGH")
                if n_med: parts.append(f"{n_med} MEDIUM")
                phase_desc = f"No active HIGH events — {', '.join(parts)} upcoming"
            
            cal_info = {
                "phase": cal.get("phase", "normal"),
                "bias": cal.get("bias", "NEUTRAL"),
                "closest_event": _s(closest_event.get("name", "")) if isinstance(closest_event, dict) else "",
                "phase_description": _s(phase_desc),
                "upcoming_events": upcoming,
            }

            # GPT Validator
            gpt_val = brain_result.gpt_validation or {}
            gpt_info = {
                "action": gpt_val.get("action", ""),
                "adjustment": gpt_val.get("adjustment", 0),
                "reason": gpt_val.get("reason", ""),
            } if gpt_val.get("action") else None

            # Confirmations & alerts from brain
            confirmations = list(brain_result.confirmations[:5]) if brain_result.confirmations else []
            alerts = list(brain_result.alerts[:5]) if brain_result.alerts else []

            # S/R Zones for dashboard (4 above + 4 below current price)
            sr_zones_display = []
            try:
                sr_zones_raw = getattr(self, '_last_sr_zones', []) or []
                cp = getattr(self, '_last_current_price', None) or 0
                above = sorted([z for z in sr_zones_raw if z.midpoint > cp], key=lambda z: z.midpoint)[:4]
                below = sorted([z for z in sr_zones_raw if z.midpoint <= cp], key=lambda z: -z.midpoint)[:4]
                PIP = 0.1
                for z in above:
                    sr_zones_display.append({
                        "price": round(z.midpoint, 2), "zone_type": z.zone_type,
                        "touches": z.touches, "timeframe": z.timeframe,
                        "dist_pips": round(abs(z.midpoint - cp) / PIP, 0),
                        "position": "above",
                        "confluence": z.confluence,
                        "strength": z.strength,
                    })
                for z in below:
                    sr_zones_display.append({
                        "price": round(z.midpoint, 2), "zone_type": z.zone_type,
                        "touches": z.touches, "timeframe": z.timeframe,
                        "dist_pips": round(abs(cp - z.midpoint) / PIP, 0),
                        "position": "below",
                        "confluence": z.confluence,
                        "strength": z.strength,
                    })
            except Exception:
                pass

            # Candlestick patterns for dashboard
            candlestick_patterns_display = None
            try:
                patterns_data = getattr(self, '_last_candlestick_patterns', None)
                if patterns_data and patterns_data.get("primary_pattern"):
                    primary = patterns_data["primary_pattern"]
                    sr_ctx = patterns_data.get("sr_context")
                    sr_context_str = ""
                    if sr_ctx:
                        sr_context_str = f"Near {sr_ctx['timeframe']} {sr_ctx['zone_type'].lower()} @ {sr_ctx['price']:.2f} ({sr_ctx['touches']} touches)"
                    candlestick_patterns_display = {
                        "primary": {
                            "name": primary.get("name"),
                            "direction": primary.get("direction"),
                            "base_score": primary.get("base_score"),
                            "sr_multiplier": primary.get("sr_multiplier"),
                            "final_score": primary.get("final_score"),
                            "sr_context": sr_context_str,
                        },
                        "all_patterns": [p.get("name") for p in patterns_data.get("patterns", [])],
                    }
            except Exception:
                pass

            return {
                "headlines": headlines,
                "macro": macro,
                "anomalies": result.get("anomalies", []),
                "analysis_method": analysis_method,
                "news_score": float(news_data.get("score", 50.0)),
                "cache_age_minutes": round(cache_age_minutes, 1),
                "calendar": cal_info,
                "gpt_validator": gpt_info,
                "confirmations": confirmations,
                "alerts": alerts,
                "sr_zones": sr_zones_display,
                "candlestick_patterns": candlestick_patterns_display,
            }
        except Exception as e:
            log.debug(f"_build_intel_feed: {e}")
            return None
    
    def _monitor_cycle(self):
        """Position monitoring cycle"""
        if not self.executes_trades:
            return
        
        actions = monitor_positions()
        
        for action in actions:
            log.info(f"   Monitor: {action['action']} - Ticket {action.get('ticket', 'N/A')}")
            
            # Update statistics
            if action['action'] in ['TIMEOUT_CLOSE', 'DRAWDOWN_CLOSE', 'BROKER_CLOSE']:
                profit = action.get('profit', 0)
                is_pending = action.get('pending', False)
                
                if not is_pending:
                    # Real P&L confirmed — count in daily stats
                    self.daily_stats['trades'] += 1
                    if profit > 0:
                        self.daily_stats['wins'] += 1
                    elif profit < 0:
                        self.daily_stats['losses'] += 1
                    else:
                        self.daily_stats['breakevens'] = self.daily_stats.get('breakevens', 0) + 1
                    self.daily_stats['pnl'] += profit
                
                # Save to dashboard history
                add_closed_trade(self, {
                    "ticket": action.get("ticket"),
                    "direction": action.get("direction"),
                    "volume": action.get("volume"),
                    "open_price": action.get("open_price"),
                    "close_price": action.get("close_price"),
                    "profit": profit if not is_pending else None,
                    "reason": action.get("reason"),
                    "close_time": action.get("close_time"),
                    "close_type": action.get("close_type"),
                    "estimated": action.get("estimated", False),
                    "pending": is_pending,
                    "outcome": action.get("outcome"),
                    "orig_tp": action.get("orig_tp"),
                    "orig_sl": action.get("orig_sl"),
                })
                
                # Save to SQLite history
                close_reason = action.get("reason", "unknown")
                if is_pending:
                    close_reason = f"{close_reason} (pending)"
                record_trade_close(
                    ticket=action.get("ticket"),
                    close_price=action.get("close_price"),
                    profit=profit if not is_pending else None,
                    close_reason=close_reason,
                    close_time=action.get("close_time"),
                    breakeven_activated=action.get("breakeven_activated", False),
                )
                
                # Record for safety checks (cooldown applies even for pending)
                record_trade_result(profit)
                
                # Record close type for dynamic cooldown
                close_type = action.get('close_type', 'sl')
                trade_dir = action.get('direction', 'BUY')
                record_close_type(trade_dir, close_type)
        
        # Persist state after monitor cycle
        write_state(self)
    
    def _check_daily_reset(self):
        """Check and reset daily statistics"""
        today = datetime.now().date()
        
        if today != self.daily_stats['date']:
            # Send previous day summary
            self._send_daily_summary()
            
            # Reset
            self.daily_stats = {
                'trades': 0,
                'wins': 0,
                'losses': 0,
                'breakevens': 0,
                'pnl': 0.0,
                'date': today
            }
            self.gpt_stats = {"confirm": 0, "boost": 0, "reduce": 0, "from_cache": 0}

            # Reset daily dashboard history
            self.closed_trades_today = []
            
            log.info("Daily statistics reset")

            write_state(self)
    
    def _send_daily_summary(self):
        """Send daily summary"""
        has_trades = self.daily_stats['trades'] > 0
        has_gpt = sum(self.gpt_stats[k] for k in ("confirm", "boost", "reduce")) > 0
        
        if not has_trades and not has_gpt:
            return
        
        account_balance = get_account_balance() if self.executes_trades else config.CAPITAL_INICIAL
        pnl_percent = (self.daily_stats['pnl'] / account_balance) * 100 if account_balance > 0 else 0
        
        alert_daily_summary(
            trades_total=self.daily_stats['trades'],
            wins=self.daily_stats['wins'],
            losses=self.daily_stats['losses'],
            pnl=self.daily_stats['pnl'],
            pnl_percent=pnl_percent,
            current_balance=account_balance + self.daily_stats['pnl'],
            gpt_stats=self.gpt_stats if has_gpt else None
        )


# ============================================================================
# TEST FUNCTIONS
# ============================================================================

def run_single_analysis():
    """Run a single analysis (for testing)"""
    print("=" * 60)
    print("🧪 SINGLE ANALYSIS (TEST)")
    print("=" * 60)
    
    # Initialize MT5 for data
    import MetaTrader5 as mt5
    if not mt5.initialize():
        print("⚠️ MT5 not available")
        return
    
    # Get data
    df = get_mt5_data()
    
    if df is None:
        print("❌ No data")
        mt5.shutdown()
        return
    
    # Calculate indicators
    df = calculate_indicators(df)
    
    # Technical Score
    tech_score, tech_breakdown = calculate_technical_score(df)
    print(f"\n📊 Technical Score: {tech_score:.1f}/100")
    for k, v in tech_breakdown.items():
        print(f"   {k}: {v}")
    
    # Score News
    try:
        from news_sentiment import get_hybrid_score
        news_result = get_hybrid_score()
        news_score = news_result.get('score', 50.0)
    except:
        news_score = 50.0
    print(f"\n📰 News Score: {news_score:.1f}/100")
    
    # Score ML
    try:
        from ml_predictor import get_ml_score
        ml_score, ml_prob = get_ml_score(df)
    except:
        ml_score, ml_prob = 50.0, 0.5
    print(f"\n🤖 ML Score: {ml_score:.1f}/100 (prob: {ml_prob:.3f})")
    
    # Confluence
    result = analyze_confluence(tech_score, news_score, ml_score, ml_prob)
    
    print(f"\n🎯 CONFLUENCE:")
    print(f"   Final Score: {result.final_score:.1f}/100")
    print(f"   Decision: {result.decision}")
    print(f"   Confidence: {result.confidence}")
    print(f"   ML included: {'Yes' if result.ml_included else 'No'}")
    
    # ATR and levels
    atr = get_atr_value(df)
    current_price = df['close'].iloc[-1]
    
    print(f"\n📈 Current price: {current_price:.2f}")
    print(f"   ATR(14): {atr:.2f}")

    try:
        from central_brain import is_actionable_signal, get_trade_direction
    except Exception:
        from confluence import is_actionable_signal, get_trade_direction

    if is_actionable_signal(result.decision):
        direction = get_trade_direction(result.decision)
        levels = calculate_sl_tp(current_price, direction, atr)
        
        print(f"\n💰 LEVELS FOR {direction}:")
        print(f"   Entry: {current_price:.2f}")
        print(f"   SL: {levels.stop_loss:.2f} ({levels.sl_pips:.0f} pips)")
        print(f"   TP1: {levels.take_profit_1:.2f} ({levels.tp1_pips:.0f} pips)")
        print(f"   TP2: {levels.take_profit_2:.2f} ({levels.tp2_pips:.0f} pips)")
        print(f"   R:R 1: 1:{levels.risk_reward_1}")
        print(f"   R:R 2: 1:{levels.risk_reward_2}")
    
    mt5.shutdown()
    print("\n✅ Analysis complete!")


def test_discord_connection():
    """Test Discord connection"""
    print("🧪 Testing Discord connection...")
    result = discord.send("🤖 XAU/USD Trading Bot connection test", alert_type="info")
    print(f"   Result: {'✅ OK' if result else '❌ Failed'}")


# ============================================================================
# MAIN
# ============================================================================

def main():
    """Main function"""
    import argparse
    
    parser = argparse.ArgumentParser(description='XAU/USD Trading Bot')
    parser.add_argument('--test', action='store_true', help='Run single test analysis')
    parser.add_argument('--discord-test', action='store_true', help='Test Discord connection')
    parser.add_argument('--dry-run', action='store_true', help='Force DRY RUN mode (simulation)')
    parser.add_argument('--demo', action='store_true', help='Force DEMO mode (MT5 demo, fake $)')
    parser.add_argument('--live', action='store_true', help='Force LIVE mode (MT5 real, real $)')
    
    args = parser.parse_args()
    
    # Override mode if specified via CLI
    if args.dry_run:
        config.TRADING_MODE = "DRY_RUN"
    elif args.demo:
        config.TRADING_MODE = "DEMO"
    elif args.live:
        config.TRADING_MODE = "LIVE"
    config.DRY_RUN = (config.TRADING_MODE == "DRY_RUN")
    
    # Execute action
    if args.test:
        run_single_analysis()
    elif args.discord_test:
        test_discord_connection()
    else:
        # Run bot
        bot = TradingBot()
        bot.run()


if __name__ == "__main__":
    main()
