"""
MAIN - Main Trading Bot
Orchestrator of the XAU/USD automated trading system
"""

import sys
sys.dont_write_bytecode = True

# FLO-274: Force UTF-8 on stdout/stderr so emojis and unicode in prints/logs
# don't crash on Windows cp1252 consoles. Must run before ANY print or log call.
try:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import os
import time
import signal
import json
import subprocess
import threading
from datetime import datetime, timedelta, timezone
from tz_utils import utc_now, utc_iso, trading_day_broker_aligned, trading_day_utc
from typing import Optional, Any
import traceback

# Add directory to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# FLO-96: MT5 server time offset (EEST=UTC+3 in summer, EET=UTC+2 in winter).
# MT5 timestamps (copy_rates, tick.time) are server-local epochs, not UTC.
# Subtract this offset to convert to true UTC. Cached, refreshes every 60 min.
_mt5_offset_cache = {"value": 10800, "computed_at": 0.0}

def _mt5_server_offset() -> int:
    """Return seconds to subtract from MT5 timestamps to get true UTC epoch."""
    if time.time() - _mt5_offset_cache["computed_at"] < 3600:
        return _mt5_offset_cache["value"]
    try:
        import MetaTrader5 as _mt5_tz
        _tick = _mt5_tz.symbol_info_tick("XAUUSD")
        if _tick and _tick.time > 0:
            offset = int(_tick.time) - int(time.time())
            _mt5_offset_cache.update({"value": offset, "computed_at": time.time()})
            return offset
    except Exception:
        pass
    return _mt5_offset_cache["value"]

import config
from logger import log
from state_writer import write_state, add_closed_trade
from db_writer import init_db, record_analysis, record_trade_open, record_trade_close, record_agent_decision, get_recent_agent_decisions, get_trade_feedback, record_trade_adjustment, get_trade_adjustments
from agent_reflection import run_reflection_async
from alerts import (
    alert_bot_started, alert_bot_stopped,
    alert_safety_block, alert_error, alert_daily_summary, discord,
    alert_heartbeat_full,
    alert_market_closed, alert_market_open,
    alert_m5_reversal_block, alert_trade_resolved,
    alert_spread_delay, alert_spread_skip,
    alert_agent_decision,
    alert_proactive_decision,
    check_ea_bridge_status_and_alert
)
from confluence import analyze_confluence
from confluence import is_actionable_signal as confluence_is_actionable
from confluence import get_trade_direction as confluence_get_direction
from risk_manager import calculate_position_size, calculate_sl_tp
from safety_checks import is_safe_to_trade, record_trade_result, record_trade_opened, record_close_type, get_safety_status, is_market_open, is_bot_paused
from executor import (
    connect_mt5, disconnect_mt5, is_mt5_connected,
    get_account_balance, execute_buy, execute_sell, get_positions, close_position, executor,
    get_recent_closed_deals, get_deal_history
)
from monitor import monitor_positions, get_positions_summary, close_all_positions
from technical_analyzer import get_mt5_data, calculate_indicators, calculate_technical_score, get_atr_value
from floki_position_manager import (
    get_ea_management_params,
    get_fallback_minutes,
    get_scheduled_minutes,
    write_floki_heartbeat,
)

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
            'date': trading_day_broker_aligned()  # FLO-286: broker-midnight aligned (string YYYY-MM-DD)
        }

        self._breakeven_threshold = float(getattr(config, "BREAKEVEN_PROFIT_THRESHOLD", 0.50))

        # Closed trades today (for dashboard)
        self.closed_trades_today = []
        
        # Heartbeat tracking
        self.last_heartbeat = None
        self.last_heartbeat_scenario = None
        self.last_heartbeat_score = None
        
        # Temporary data from last analysis (for heartbeat)
        self._last_calendar_data = None
        self._last_vol_status = None
        self.current_trade = None
        self._last_deal_resolver_launch_ts_by_ticket = {}
        self._last_scenario_description = None
        self._last_gpt_validation = None

        # Sage daily auditor schedule guard (UTC date string)
        self._sage_last_run_date = None
        
        # Market state tracking (for open/close detection)
        self.market_was_open = True  # Assume open at startup
        self._last_keepalive_log = None  # Timestamp of last keepalive log (market closed)
        
        # GPT Confidence Validator stats
        self.gpt_stats = {"confirm": 0, "boost": 0, "reduce": 0, "from_cache": 0}

        self._agent_monitor = None
        self._last_agent_monitor_tick = None

        self._fast_decision_lock = threading.Lock()

        self._proactive_lock = threading.Lock()
        self._last_agent_data = None
        self._last_df = None
        self._skip_initial_proactive_h1 = False

        # Configure shutdown handler
        signal.signal(signal.SIGINT, self._shutdown_handler)
        signal.signal(signal.SIGTERM, self._shutdown_handler)

    def _validate_trade_plan_open(self, decision: str, trade_plan: dict) -> Optional[dict]:
        try:
            d = str(decision or "").strip().upper()
            if d not in ("OPEN_BUY", "OPEN_SELL"):
                return None

            if not isinstance(trade_plan, dict):
                return {"ok": False, "reason": "missing_trade_plan"}

            entry = trade_plan.get("entry") or trade_plan.get("entry_price")
            sl = trade_plan.get("stop_loss") or trade_plan.get("sl")
            tp = trade_plan.get("take_profit") or trade_plan.get("tp")

            try:
                entry_f = float(entry)
                sl_f = float(sl)
                tp_f = float(tp)
            except Exception:
                return {"ok": False, "reason": "non_numeric_entry_sl_tp"}

            pip = 0.1
            sl_pips = abs(entry_f - sl_f) / pip
            if sl_pips < 50 or sl_pips > 800:
                return {"ok": False, "reason": f"sl_distance_out_of_range_pips:{sl_pips:.1f}"}

            return {
                "ok": True,
                "entry": entry_f,
                "stop_loss": sl_f,
                "take_profit": tp_f,
                "sl_pips": sl_pips,
            }
        except Exception as e:
            return {"ok": False, "reason": f"validation_error:{e}"}

    def _get_last_proactive_analysis_timestamp_iso(self) -> Optional[str]:
        try:
            from db_writer import get_last_agent_proactive_timestamp

            ts = get_last_agent_proactive_timestamp()
            if isinstance(ts, str) and ts.strip():
                return ts.strip()
        except Exception:
            pass

        try:
            la = self.last_analysis if isinstance(self.last_analysis, dict) else {}
            pa = la.get("proactive_analysis") if isinstance(la.get("proactive_analysis"), dict) else {}
            ts = pa.get("timestamp")
            if isinstance(ts, str) and ts.strip():
                return ts.strip()
        except Exception:
            pass

        return None

    def _parse_iso_datetime(self, iso_str: str) -> Optional[datetime]:
        if not isinstance(iso_str, str) or not iso_str.strip():
            return None
        s = iso_str.strip()
        try:
            if s.endswith("Z"):
                s = s[:-1] + "+00:00"
        except Exception:
            pass
        try:
            return datetime.fromisoformat(s)
        except Exception:
            return None

    
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

            if isinstance(daily_stats, dict) and daily_stats:
                self.daily_stats = daily_stats

            lkp = payload.get("last_known_price")
            if lkp is not None:
                try:
                    self.last_known_price = float(lkp)
                except Exception:
                    pass

            trade_history = payload.get("trade_history")
            if isinstance(trade_history, list):
                self.closed_trades_today = trade_history
        except Exception as e:
            log.debug(f"Failed to load persisted dashboard state: {e}")

    def agent_proactive_out_of_cycle(self, trigger_type: str, trigger_data: dict) -> dict:
        # FLO-241: Reset progressive backoff on Simba wake (not scheduled cycles)
        if str(trigger_type or "") in ("SIMBA_WAKE", "SIMBA_WATCH"):
            self._consecutive_no_timer = 0

        acquired = False
        try:
            try:
                acquired = self._proactive_lock.acquire(blocking=False)
            except Exception:
                acquired = True

            # FLO-90: ECHO_CRITICAL removed — Echo is pull-only, no forced cycles
            allowed = {"SCHEDULED", "SIMBA_WAKE", "SIMBA_WATCH", "PENDING_FILL"}
            if str(trigger_type or "") not in allowed:
                log.info(f"FLOKI_SCHEDULE | Blocked legacy trigger: {trigger_type}")
                return {"success": False, "reason": "blocked_legacy_trigger", "trigger_type": trigger_type}

            if not acquired:
                log.info("MONITOR | Out-of-cycle Proactive skipped — analysis already running")
                return {"success": False, "reason": "proactive_in_progress"}

            agent_data = getattr(self, "_last_agent_data", None)
            df = getattr(self, "_last_df", None)

            if not agent_data or not isinstance(agent_data, dict):
                return {"success": False, "reason": "missing_agent_data"}
            if df is None or len(df) < 50:
                return {"success": False, "reason": "missing_df"}

            snapshot_time_iso = utc_iso()  # FLO-286: UTC ISO with Z suffix
            self._call_agent_proactive_snapshot(
                trigger_type=str(trigger_type or ""),
                snapshot_time_iso=snapshot_time_iso,
                agent_data=agent_data,
                df=df,
                trigger_data=trigger_data if isinstance(trigger_data, dict) else {},
            )

            try:
                write_state(self)
            except Exception:
                pass

            return {"success": True}
        except Exception as e:
            log.warning(f"PROACTIVE_OOC | error (non-blocking): {e}")
            return {"success": False, "reason": str(e)}
        finally:
            if acquired:
                try:
                    self._proactive_lock.release()
                except Exception:
                    pass

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
            today = trading_day_broker_aligned()  # FLO-286: broker-midnight aligned

            saved_pnl = float(self.daily_stats.get('pnl', 0.0) or 0.0)
            saved_date = self.daily_stats.get('date')

            # If saved state is from another day, clear (daily reset will handle)
            if saved_date and str(saved_date) != today:
                log.info(f"Reconciliation: saved state is from {saved_date}, today is {today} — daily reset will fix")
                return
            
            # Get ALL real closing deals from MT5 (last 7 days + today)
            real_deals = get_recent_closed_deals(hours=168)
            
            # Index real deals by position_id
            real_deals_by_pos = {}
            for d in real_deals:
                real_deals_by_pos[d['position_id']] = d
            
            # Separate today's deals vs historical by CLOSE day, broker-aligned.
            # FLO-286 + FLO-333: `today` comes from trading_day_broker_aligned()
            # (broker midnight = 22:00 UTC). Deal close_times from executor are
            # naive-UTC datetimes (executor.py:1526). Must apply the same
            # broker-offset shift to each deal's close before comparing —
            # otherwise a trade closing at 22:52 UTC Apr 15 (= broker 00:52
            # Apr 16) gets dropped from Apr 16's "Trades Today" because its
            # raw UTC day is Apr 15.
            def _trade_day(d):
                ct = d.get('close_time')
                dt = ct if ct else d['open_time']
                return trading_day_broker_aligned(now=dt)

            today_deals = [d for d in real_deals if _trade_day(d) == today]
            today_tickets = {d['position_id'] for d in today_deals}
            historical_deals = [d for d in real_deals if d['position_id'] not in today_tickets]
            
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
                # Derive close_type from reason + P&L heuristic.
                # On restart, monitor state is lost — MT5 only reports reason strings.
                # FLO-290: "Expert Advisor" = bot sent MarketClose (Floki close_trade
                # or monitor risk close), NEVER a SL hit (FlokiBridge is a pure
                # executor and doesn't self-trigger SL/TP). Previously this fell
                # into the else branch and got mislabeled "sl".
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
                elif deal_reason == "Expert Advisor":
                    deal_close_type = "floki_close"
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
                    breakeven_activated=False,  # FLO-220: reconciliation — data not available from MT5
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
                
                # ============================================================
                # FLO-97: Backfill today's AND historical deals missing from SQLite
                # Catches trades where ticket=0 was returned at execution time
                # ============================================================
                backfill_count = 0
                for deal in today_deals:
                    pos_id = deal['position_id']
                    if pos_id <= 0 or pos_id in all_sqlite_tickets:
                        continue

                    # Derive decision_source from comment
                    deal_comment = deal.get('comment', '') or ''
                    if deal_comment.startswith('Brain-') or deal_comment.startswith('Bot-') or deal_comment.startswith('Agent-'):
                        dec_source = 'floki_agent'
                    else:
                        dec_source = None

                    # Open price from entry deal; warn if missing
                    open_px = deal.get('open_price')
                    recon_comment = f"reconciled:{deal_comment}"
                    if open_px is None:
                        open_px = deal['close_price']
                        recon_comment = f"reconciled(estimated):{deal_comment}"
                        log.warning(f"RECONCILIATION | #{pos_id}: open_price unavailable, using close_price as estimate")

                    record_trade_open(
                        ticket=pos_id, direction=deal['direction'],
                        volume=deal['volume'],
                        open_price=open_px,
                        sl=0, tp=0,
                        open_time=deal['close_time'].isoformat(),
                        comment=recon_comment,
                        decision_source=dec_source,
                    )
                    record_trade_close(
                        ticket=pos_id, close_price=deal['close_price'],
                        profit=deal['profit'], close_reason=deal['reason'],
                        close_time=deal['close_time'].isoformat(),
                        breakeven_activated=False,  # FLO-220: backfill — data not available
                    )
                    all_sqlite_tickets.add(pos_id)
                    backfill_count += 1
                    log.info(
                        f"RECONCILIATION | Backfilled #{pos_id}: {deal['direction']} | "
                        f"open={open_px} → close={deal['close_price']:.2f} | "
                        f"P&L=${deal['profit']:+.2f} | {deal['reason']} | source=MT5 deal history"
                    )
                    try:
                        alert_error(
                            "Trade Backfill",
                            f"Trade #{pos_id} ({deal['direction']}) was missing from DB — backfilled from MT5. P&L=${deal['profit']:+.2f}",
                            impact="Trade now visible to Sage, reflection engine, and performance tracking",
                            severity="warning",
                        )
                    except Exception:
                        pass

                # Also backfill historical deals not limited to Bot- comments
                for deal in historical_deals:
                    pos_id = deal['position_id']
                    if pos_id <= 0 or pos_id in all_sqlite_tickets:
                        continue
                    deal_comment = deal.get('comment', '') or ''
                    if not (deal_comment.startswith('Brain-') or deal_comment.startswith('Bot-') or deal_comment.startswith('Agent-')):
                        continue

                    dec_source = 'floki_agent'
                    open_px = deal.get('open_price')
                    recon_comment = f"reconciled:{deal_comment}"
                    if open_px is None:
                        open_px = deal['close_price']
                        recon_comment = f"reconciled(estimated):{deal_comment}"

                    record_trade_open(
                        ticket=pos_id, direction=deal['direction'],
                        volume=deal['volume'],
                        open_price=open_px,
                        sl=0, tp=0,
                        open_time=deal['close_time'].isoformat(),
                        comment=recon_comment,
                        decision_source=dec_source,
                    )
                    record_trade_close(
                        ticket=pos_id, close_price=deal['close_price'],
                        profit=deal['profit'], close_reason=deal['reason'],
                        close_time=deal['close_time'].isoformat(),
                        breakeven_activated=False,  # FLO-220: historical backfill — data not available
                    )
                    all_sqlite_tickets.add(pos_id)
                    backfill_count += 1
                    log.info(
                        f"RECONCILIATION | Backfilled historical #{pos_id}: {deal['direction']} | "
                        f"P&L=${deal['profit']:+.2f} | {deal['reason']} | {deal['close_time'].strftime('%m-%d %H:%M')}"
                    )

                if backfill_count:
                    log.info(f"RECONCILIATION | {backfill_count} trades backfilled from MT5 deal history")
                    try:
                        alert_error(
                            "Reconciliation Summary",
                            f"{backfill_count} trade(s) were missing from history.db and have been backfilled from MT5 deal history",
                            severity="warning",
                        )
                    except Exception:
                        pass

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
                                breakeven_activated=False,  # FLO-220: stale trade fix — data not available
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
                                                    breakeven_activated=False,  # FLO-220: direct deal lookup — data not available
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
            be_thr = float(getattr(self, "_breakeven_threshold", 0.50))
            new_wins = sum(1 for t in confirmed_trades if float(t.get('profit', 0) or 0) >= be_thr)
            new_losses = sum(1 for t in confirmed_trades if float(t.get('profit', 0) or 0) <= -be_thr)
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
            resolved_path = os.path.join("data", "deal_resolved.json")
            try:
                if os.path.exists(resolved_path):
                    with open(resolved_path, "r", encoding="utf-8") as f:
                        resolved_payload = json.load(f)

                    if isinstance(resolved_payload, dict) and resolved_payload.get("resolved") is True:
                        resolved_ticket = resolved_payload.get("ticket")
                        for t in (getattr(self, "closed_trades_today", []) or []):
                            if t.get("ticket") == resolved_ticket:
                                if t.get("close_price") is None:
                                    t["close_price"] = resolved_payload.get("close_price")
                                if t.get("close_time") is None:
                                    t["close_time"] = resolved_payload.get("close_time")
                                if t.get("reason") is None or str(t.get("reason")).strip() == "":
                                    t["reason"] = resolved_payload.get("reason")
                                if t.get("pending"):
                                    t["pending"] = False
                                if t.get("estimated"):
                                    t["estimated"] = False

                                try:
                                    if resolved_payload.get("close_price") is not None or resolved_payload.get("reason") is not None:
                                        log.info(
                                            f"RESOLVE_PENDING | resolved ticket #{resolved_ticket}: "
                                            f"close_price={resolved_payload.get('close_price')}, reason={resolved_payload.get('reason')}"
                                        )
                                    record_trade_close(
                                        ticket=resolved_ticket,
                                        close_price=t.get("close_price"),
                                        profit=t.get("profit"),
                                        close_reason=t.get("reason"),
                                        close_time=t.get("close_time"),
                                        breakeven_activated=False,  # FLO-220: pending resolution — data not available
                                    )
                                except Exception:
                                    pass
                                break
                    try:
                        os.remove(resolved_path)
                    except Exception:
                        pass
            except Exception:
                pass

            closed_trades = getattr(self, "closed_trades_today", []) or []
            def _needs_deal_details(trade: dict) -> bool:
                try:
                    if trade.get("close_price") is None:
                        return True
                    r = trade.get("reason")
                    if r is None:
                        return True
                    rs = str(r)
                    if "estimated" in rs.lower():
                        return True
                    if rs.strip() == "Closed by broker (details unavailable)":
                        return True
                    return False
                except Exception:
                    return False

            needs_details = [t for t in closed_trades if _needs_deal_details(t)]
            if not needs_details:
                return

            try:
                log.info(
                    f"RESOLVE_PENDING | checked {len(closed_trades)} closed trades | {len(needs_details)} need deal details"
                )
            except Exception:
                pass

            resolved_any = False

            for trade in needs_details:
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
                    if trade.get('close_price') is None:
                        trade['close_price'] = deal.get('close_price')
                    if trade.get('reason') is None or str(trade.get('reason')).strip() == "" or "estimated" in str(trade.get('reason')).lower() or str(trade.get('reason')).strip() == "Closed by broker (details unavailable)":
                        trade['reason'] = deal.get('reason')
                    if trade.get('close_time') is None:
                        trade['close_time'] = deal.get('close_time', datetime.now())
                    if trade.get('pending'):
                        trade['pending'] = False
                    if trade.get('estimated'):
                        trade['estimated'] = False
                    resolved_any = True

                    log.info(f"RESOLVE_PENDING | resolved ticket #{ticket}: close_price={trade.get('close_price')}, reason={trade.get('reason')}")
                    
                    # Update SQLite
                    record_trade_close(
                        ticket=ticket,
                        close_price=trade.get('close_price'),
                        profit=trade.get('profit'),
                        close_reason=trade.get('reason'),
                        close_time=trade.get('close_time').isoformat() if hasattr(trade.get('close_time', ''), 'isoformat') else str(trade.get('close_time', '')),
                        breakeven_activated=False,  # FLO-220: periodic deal lookup — data not available
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

                    # Fresh MT5 connection workaround (fire-and-forget)
                    try:
                        now_ts = time.time()
                        last_ts = self._last_deal_resolver_launch_ts_by_ticket.get(int(ticket))
                        if last_ts is not None and (now_ts - float(last_ts)) < 120.0:
                            continue
                        self._last_deal_resolver_launch_ts_by_ticket[int(ticket)] = now_ts

                        base_dir = os.path.dirname(os.path.abspath(__file__))
                        resolver_py = os.path.join(base_dir, "deal_resolver.py")
                        if os.path.exists(resolver_py):
                            creationflags = 0
                            if os.name == "nt":
                                creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
                            log.info(f"RESOLVE_PENDING | launching deal_resolver.py for ticket #{ticket} (profit known, details missing)")
                            subprocess.Popen(
                                [sys.executable, resolver_py, str(ticket)],
                                cwd=base_dir,
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL,
                                creationflags=creationflags,
                            )
                    except Exception:
                        pass
            
            if resolved_any:
                # Rebuild daily_stats
                confirmed = [t for t in self.closed_trades_today if not t.get('pending', False)]
                still_pending = [t for t in self.closed_trades_today if t.get('pending', False)]
                self.daily_stats['trades'] = len(confirmed)
                be_thr = float(getattr(self, "_breakeven_threshold", 0.50))
                self.daily_stats['wins'] = sum(1 for t in confirmed if float(t.get('profit', 0) or 0) >= be_thr)
                self.daily_stats['losses'] = sum(1 for t in confirmed if float(t.get('profit', 0) or 0) <= -be_thr)
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

        # FLO-SAFETY: Clear stale agent timer on startup so Floki doesn't
        # fire immediately from a schedule left by a previous session.
        try:
            _next_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "agent_next_check.json")
            if os.path.exists(_next_path):
                _startup_delay_min = 2  # give the bot time to stabilize
                _new_check = datetime.utcnow() + timedelta(minutes=_startup_delay_min)
                _payload = {
                    "next_check_at": _new_check.isoformat(timespec="seconds") + "Z",
                    "requested_minutes": _startup_delay_min,
                }
                _tmp = _next_path + ".tmp"
                with open(_tmp, "w", encoding="utf-8") as _f:
                    json.dump(_payload, _f, ensure_ascii=False, indent=2)
                os.replace(_tmp, _next_path)
                log.info(f"STARTUP | Reset agent timer — first Floki call in {_startup_delay_min} minutes")
        except Exception as e:
            log.debug(f"STARTUP | agent timer reset failed (ignored): {e}")

        # L2 Reflection engine (warm memory)
        try:
            run_reflection_async("startup")
        except Exception:
            pass

        # FLO-138 Phase 2: sync ChromaDB semantic memory on startup
        try:
            from trade_reflexion import sync_chromadb_on_startup
            sync_chromadb_on_startup()
        except Exception:
            pass

        # FLO-263: Log existing pending orders at startup (do NOT cancel — Floki placed them)
        try:
            if getattr(config, "PENDING_ORDERS_ENABLED", False):
                import MetaTrader5 as _mt5_startup
                _pending_startup = _mt5_startup.orders_get(symbol=config.SYMBOL)
                _our_pending = [o for o in (_pending_startup or []) if o.magic == config.MAGIC_NUMBER]
                if _our_pending:
                    log.info(f"STARTUP | Found {len(_our_pending)} pending orders (preserving)")
                    for _po in _our_pending:
                        _type_names = {2: "BUY_LIMIT", 3: "SELL_LIMIT", 4: "BUY_STOP", 5: "SELL_STOP"}
                        log.info(f"STARTUP | Pending: #{_po.ticket} {_type_names.get(_po.type, '?')} @ {_po.price_open} SL={_po.sl} TP={_po.tp}")
        except Exception:
            pass

        try:
            threshold_min = int(getattr(config, "STARTUP_SKIP_THRESHOLD_MINUTES", 30) or 30)
            last_ts_iso = self._get_last_proactive_analysis_timestamp_iso()
            last_dt = self._parse_iso_datetime(last_ts_iso) if last_ts_iso else None
            if last_dt is not None:
                if last_dt.tzinfo is not None:
                    now_dt = datetime.now(timezone.utc)
                    last_dt = last_dt.astimezone(timezone.utc)
                else:
                    now_dt = datetime.now()

                delta_min = (now_dt - last_dt).total_seconds() / 60.0
                skip = bool(delta_min >= 0 and delta_min < float(threshold_min))
                log.info(
                    f"STARTUP | last_proactive_ts={last_ts_iso} | age_minutes={delta_min:.2f} | "
                    f"threshold={threshold_min} | skip={skip}"
                )
                if skip:
                    self._skip_initial_proactive_h1 = True
                    log.info(
                        f"STARTUP | Skipping initial Floki call — last analysis was {int(delta_min)}m ago (threshold: {threshold_min}m)"
                    )

                    try:
                        last_closed_h1_iso = self._get_last_closed_h1_time_iso()
                        if last_closed_h1_iso:
                            self._last_proactive_h1_close_time = last_closed_h1_iso
                    except Exception:
                        pass
        except Exception as e:
            log.debug(f"STARTUP | skip proactive check error (ignored): {e}")
        
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
        log.info(f"Central Scanner: {'ON' if config.USE_CENTRAL_BRAIN else 'OFF (confluence)'}")
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
                    log.warning("AI Agent: OFF (init failed)")
            except Exception as e:
                log.warning(f"AI Agent init error (ignored): {e}")

        # Give the PositionMonitor access to this bot instance (for Agent watch-condition triggers)
        try:
            from monitor import monitor as _monitor_instance
            _monitor_instance.bot = self
        except Exception:
            pass

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

        # FLO-236: Deep Search — run immediately on start (non-blocking)
        try:
            from deep_search import run_deep_search
            threading.Thread(target=run_deep_search, daemon=True).start()
        except Exception:
            pass

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

                # Sage daily auditor (non-blocking, UTC schedule, skip weekends)
                try:
                    use_sage = bool(getattr(config, "USE_SAGE_AUDITOR", False))
                    run_time = str(getattr(config, "SAGE_RUN_TIME_UTC", "21:00") or "21:00").strip()
                    if use_sage and run_time:
                        now_utc = datetime.utcnow()
                        # Skip weekends
                        if now_utc.weekday() < 5:
                            parts = run_time.split(":")
                            hh = int(parts[0]) if len(parts) >= 1 else 21
                            mm = int(parts[1]) if len(parts) >= 2 else 0
                            hh = max(0, min(23, hh))
                            mm = max(0, min(59, mm))

                            today = now_utc.date().isoformat()
                            due = (now_utc.hour, now_utc.minute) >= (hh, mm)
                            not_run_today = (getattr(self, "_sage_last_run_date", None) != today)

                            if due and not_run_today:
                                setattr(self, "_sage_last_run_date", today)

                                def _run_sage_safe() -> None:
                                    try:
                                        from sage_auditor import run_sage_auditor

                                        run_sage_auditor()

                                        # FLO-269: EOD counterfactual replay for post-trade reports
                                        try:
                                            from trade_reflexion import run_eod_counterfactuals
                                            run_eod_counterfactuals()
                                        except Exception as e_cf:
                                            log.debug(f"EOD_COUNTERFACTUAL | error (ignored): {e_cf}")

                                        # FLO-113: Update sage_last_run.json so dashboard shows correct date
                                        try:
                                            import json as _json
                                            from datetime import datetime as _dt, timezone as _tz
                                            _now = _dt.now(_tz.utc)
                                            _last_run_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "sage_last_run.json")
                                            _tmp = _last_run_path + ".tmp"
                                            with open(_tmp, "w", encoding="utf-8") as _f:
                                                _json.dump({
                                                    "last_run_date": _now.date().isoformat(),
                                                    "last_run_time": _now.strftime("%H:%M:%S"),
                                                }, _f)
                                            os.replace(_tmp, _last_run_path)
                                        except Exception:
                                            pass
                                    except Exception as e_sage:
                                        log.warning(f"SAGE | scheduler error (ignored): {e_sage}")

                                threading.Thread(target=_run_sage_safe, daemon=True).start()
                except Exception as e:
                    log.debug(f"SAGE | schedule check error (ignored): {e}")

                # Echo News Sentinel (non-blocking, every ECHO_SCAN_INTERVAL_SECONDS)
                # FLO-92: Skip Echo scans when bot is paused
                try:
                    echo_enabled = bool(getattr(config, "ECHO_ENABLED", False))
                    if echo_enabled:
                        echo_interval = int(getattr(config, "ECHO_SCAN_INTERVAL_SECONDS", 300))
                        now_ts = time.time()
                        last_echo = getattr(self, "_echo_last_scan_ts", 0) or 0

                        if (now_ts - last_echo) >= echo_interval:
                            self._echo_last_scan_ts = now_ts

                            def _run_echo_safe() -> None:
                                try:
                                    from echo_sentinel import run_echo_scan
                                    result = run_echo_scan()

                                    # FLO-90: Echo is pull-only. Log CRITICAL alerts but do NOT
                                    # wake Floki or Luna. Floki reads alerts via get_echo_alerts
                                    # during his normal scheduled cycles.
                                    if result.critical_alerts:
                                        fresh_critical = [c for c in result.critical_alerts if c.age_hours < 1.0]
                                        if fresh_critical:
                                            titles = "; ".join(c.title[:60] for c in fresh_critical[:3])
                                            log.info(f"ECHO | {len(fresh_critical)} CRITICAL alert(s) stored (pull-only, no wake): {titles}")
                                except Exception as e_echo:
                                    log.warning(f"ECHO | scheduler error (ignored): {e_echo}")

                            threading.Thread(target=_run_echo_safe, daemon=True).start()
                except Exception as e:
                    log.debug(f"ECHO | schedule check error (ignored): {e}")

                # Luna Macro Analyst (non-blocking, every LUNA_SCAN_INTERVAL_SECONDS)
                # FLO-92: Skip Luna scans when bot is paused
                try:
                    luna_enabled = bool(getattr(config, "LUNA_ENABLED", False))
                    if luna_enabled:
                        _luna_market_open, _luna_reason, _luna_next_open = is_market_open()
                        _luna_is_weekend = (not _luna_market_open
                                            and 'weekend' in (_luna_reason or '').lower())

                        # Weekend: sleep completely unless within 1h of market open (pre-market brief)
                        _luna_skip = False
                        if _luna_is_weekend:
                            if _luna_next_open is not None:
                                _seconds_to_open = (_luna_next_open - datetime.utcnow()).total_seconds()
                                if _seconds_to_open > 3600:
                                    _luna_skip = True
                                    # Log once per hour max
                                    _luna_last_weekend_log = getattr(self, "_luna_last_weekend_log", 0) or 0
                                    if (time.time() - _luna_last_weekend_log) > 3600:
                                        self._luna_last_weekend_log = time.time()
                                        _open_str = _luna_next_open.strftime('%A %H:%M UTC') if _luna_next_open else 'unknown'
                                        log.info(f"LUNA | Weekend — sleeping (next run at {_open_str})")
                            else:
                                _luna_skip = True

                        if not _luna_skip:
                            # Market open: 15 min. Daily pause / pre-market: 30 min
                            luna_interval = int(getattr(
                                config,
                                "LUNA_SCAN_INTERVAL_SECONDS" if _luna_market_open else "LUNA_SCAN_INTERVAL_CLOSED",
                                900 if _luna_market_open else 1800,
                            ))
                            now_ts = time.time()
                            last_luna = getattr(self, "_luna_last_scan_ts", 0) or 0

                            if (now_ts - last_luna) >= luna_interval:
                                self._luna_last_scan_ts = now_ts

                                def _run_luna_safe() -> None:
                                    try:
                                        from luna_analyst import run_luna_analysis
                                        result = run_luna_analysis()
                                        log.info(
                                            f"LUNA | {result.source} — {result.environment} | "
                                            f"risk {result.risk_level}/10 | bias {result.directional_bias}"
                                        )
                                    except Exception as e_luna:
                                        log.warning(f"LUNA | scheduler error (ignored): {e_luna}")

                                threading.Thread(target=_run_luna_safe, daemon=True).start()
                except Exception as e:
                    log.debug(f"LUNA | schedule check error (ignored): {e}")

                # --- Deep Search refresh (FLO-236) ---
                try:
                    _ds_now = time.time()
                    _ds_last = getattr(self, "_deep_search_last_ts", 0) or 0
                    if (_ds_now - _ds_last) >= 7200:
                        self._deep_search_last_ts = _ds_now
                        from deep_search import run_deep_search
                        threading.Thread(target=run_deep_search, daemon=True).start()
                except Exception:
                    pass

                # --- Rex Monitor (FLO-211) ---
                try:
                    _rex_mon_enabled = getattr(config, "REX_MONITOR_ENABLED", False)
                    if _rex_mon_enabled:
                        _rex_interval = getattr(config, "REX_MONITOR_INTERVAL", 1800)
                        _rex_interval_closed = getattr(config, "REX_MONITOR_INTERVAL_CLOSED", 3600)
                        _rex_now = time.time()
                        _rex_last = getattr(self, "_rex_monitor_last_ts", 0) or 0
                        _rex_use_interval = _rex_interval  # default: market open

                        # Use closed interval if market is not open
                        try:
                            _mo, _, _ = is_market_open()
                            if not _mo:
                                _rex_use_interval = _rex_interval_closed
                        except Exception:
                            pass

                        if (_rex_now - _rex_last) >= _rex_use_interval:
                            self._rex_monitor_last_ts = _rex_now

                            def _run_rex_monitor_safe() -> None:
                                try:
                                    from rex_monitor import run_rex_monitor
                                    from agent_tools import AgentTools
                                    import safety_checks as _sc
                                    import risk_manager as _rm
                                    _tools = AgentTools(
                                        self,
                                        executor=getattr(self, "executor", None),
                                        safety_checks_module=_sc,
                                        risk_manager_module=_rm,
                                    )
                                    run_rex_monitor(_tools)
                                except Exception as e_rex:
                                    log.warning(f"REX_MONITOR | scheduler error (ignored): {e_rex}")

                            threading.Thread(target=_run_rex_monitor_safe, daemon=True).start()
                except Exception as e:
                    log.debug(f"REX_MONITOR | schedule check error (ignored): {e}")

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
                # Scanner + Monitor must run every 60s for fresh data.
                interval = 60
                elapsed = 0
                monitor_interval = int(getattr(config, "MONITOR_INTERVAL_SECONDS", 10) or 10)

                while elapsed < interval and self.running:
                    sleep_time = min(monitor_interval, interval - elapsed)
                    for _ in range(int(sleep_time)):
                        if not self.running:
                            break
                        time.sleep(1)
                    elapsed += int(sleep_time)

                    if self.running:
                        # A/B test resolution (every 30 min)
                        try:
                            _ab_last = getattr(self, '_ab_test_last_resolve', 0)
                            if time.time() - _ab_last >= 1800 and getattr(config, 'AB_TEST_ENABLED', False):
                                self._resolve_ab_test_entries()
                                self._ab_test_last_resolve = time.time()
                        except Exception:
                            pass

                        try:
                            now_ts = time.time()
                            last_ts = self._last_agent_monitor_tick or 0
                            if (now_ts - last_ts) >= 30:
                                if self._agent_monitor is None:
                                    from agent_monitor import AgentMonitor
                                    self._agent_monitor = AgentMonitor(bot=self)
                                self._agent_monitor.check()
                                self._last_agent_monitor_tick = now_ts
                        except Exception as e:
                            log.debug(f"AGENT_MONITOR | tick error (ignored): {e}")

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
                    log.error(f"⚠️ Scanner failed! Error: {e}")
                    log.error(traceback.format_exc())
                    log.warning("Using confluence as fallback...")
                    alert_error("Scanner Degraded", f"Scanner failed: {e}. Using confluence as fallback.")
                    decision, final_score, confidence, direction, tech_score, news_score, ml_score, explanation, agent_data = \
                        self._confluence_analysis(df)
            else:
                decision, final_score, confidence, direction, tech_score, news_score, ml_score, explanation, agent_data = \
                    self._confluence_analysis(df)

            try:
                self._last_agent_data = agent_data
                self._last_df = df
            except Exception:
                pass

            # ------------------------------------------------------------
            # AGENT TOOL CACHE BRIDGE (non-blocking)
            # The Scanner returns agent_data with keys like tech_data/news_data/calendar_data,
            # while AgentTools expects dp['indicators'], dp['macro'], dp['calendar'], etc.
            # Enrich the cached dict in-place so tool-driven Agent investigations have data.
            # ------------------------------------------------------------
            try:
                dp = agent_data if isinstance(agent_data, dict) else None
                if dp is not None:
                    # Wire candlestick patterns for Simba scanner_pattern conditions
                    dp["patterns"] = getattr(self, '_last_candlestick_patterns', None)

                    tech_data = dp.get("tech_data") if isinstance(dp.get("tech_data"), dict) else {}
                    momentum_data = dp.get("momentum_data") if isinstance(dp.get("momentum_data"), dict) else {}
                    news_data = dp.get("news_data") if isinstance(dp.get("news_data"), dict) else {}
                    calendar_data = dp.get("calendar_data") if isinstance(dp.get("calendar_data"), dict) else {}
                    ml_data = dp.get("ml_data") if isinstance(dp.get("ml_data"), dict) else {}

                    # indicators
                    if not isinstance(dp.get("indicators"), dict) or not dp.get("indicators"):
                        indicators = {}

                        def _df_get(col_name: str):
                            try:
                                if df is None or not hasattr(df, "columns"):
                                    return None
                                if col_name not in df.columns:
                                    return None
                                return df[col_name].iloc[-1]
                            except Exception:
                                return None
                        try:
                            indicators["rsi"] = tech_data.get("rsi") if isinstance(tech_data.get("rsi"), dict) else {}
                        except Exception:
                            indicators["rsi"] = {}
                        try:
                            macd = tech_data.get("macd") if isinstance(tech_data.get("macd"), dict) else {}

                            macd_value = None
                            try:
                                # Most detailed analyzer provides macd_val separately from signal/hist.
                                macd_value = macd.get("value")
                                if macd_value is None:
                                    macd_value = macd.get("macd")
                            except Exception:
                                macd_value = None

                            macd_signal = None
                            try:
                                macd_signal = macd.get("signal")
                            except Exception:
                                macd_signal = None

                            macd_hist = None
                            try:
                                macd_hist = macd.get("histogram")
                                if macd_hist is None:
                                    macd_hist = macd.get("macd_hist")
                            except Exception:
                                macd_hist = None

                            indicators["macd"] = {
                                "value": macd_value,
                                "signal": macd_signal,
                                "histogram": macd_hist,
                            }

                            # If analyzer only provides histogram/signal label, backfill MACD line from df columns.
                            try:
                                if indicators.get("macd") and indicators["macd"].get("value") is None:
                                    dv = _df_get("macd")
                                    if dv is not None:
                                        indicators["macd"]["value"] = float(dv)
                            except Exception:
                                pass
                        except Exception:
                            indicators["macd"] = {}
                        try:
                            ema = tech_data.get("ema") if isinstance(tech_data.get("ema"), dict) else {}

                            # Support both legacy keys and technical_analyzer.analyze_technical_detailed shape.
                            ema50 = None
                            ema200 = None
                            try:
                                ema50 = ema.get("ema50")
                                if ema50 is None:
                                    ema50 = ema.get("ema_50")
                            except Exception:
                                ema50 = None
                            try:
                                ema200 = ema.get("ema200")
                                if ema200 is None:
                                    ema200 = ema.get("ema_200")
                            except Exception:
                                ema200 = None

                            # If analyzer only provides above flags, do not fabricate numeric EMA.
                            indicators["emas"] = {
                                "ema50": ema50,
                                "ema200": ema200,
                            }

                            # Backfill ema50 from df if missing (technical_analyzer calculates ema_50).
                            try:
                                if indicators.get("emas") and indicators["emas"].get("ema50") is None:
                                    ev = _df_get("ema_50")
                                    if ev is not None:
                                        indicators["emas"]["ema50"] = float(ev)
                            except Exception:
                                pass
                        except Exception:
                            indicators["emas"] = {}
                        try:
                            atr_block = (momentum_data.get("atr") if isinstance(momentum_data.get("atr"), dict) else {})
                            atr_val = atr_block.get("atr_value") if "atr_value" in atr_block else atr_block.get("atr_current")
                            indicators["atr"] = {"value": atr_val}
                        except Exception:
                            indicators["atr"] = {}
                        try:
                            adx_block = (momentum_data.get("adx") if isinstance(momentum_data.get("adx"), dict) else {})
                            indicators["adx"] = {
                                "value": adx_block.get("adx_value"),
                                "plus_di": adx_block.get("plus_di"),
                                "minus_di": adx_block.get("minus_di"),
                            }
                        except Exception:
                            indicators["adx"] = {}
                        try:
                            bb = tech_data.get("bollinger") if isinstance(tech_data.get("bollinger"), dict) else {}

                            # Most analyzer provides bb position/width/squeeze; prefer explicit bands if present.
                            bb_upper = None
                            bb_middle = None
                            bb_lower = None
                            bb_pos = None
                            try:
                                bb_upper = bb.get("upper")
                                if bb_upper is None:
                                    bb_upper = bb.get("bb_upper")
                            except Exception:
                                bb_upper = None
                            try:
                                bb_middle = bb.get("middle")
                                if bb_middle is None:
                                    bb_middle = bb.get("bb_middle")
                            except Exception:
                                bb_middle = None
                            try:
                                bb_lower = bb.get("lower")
                                if bb_lower is None:
                                    bb_lower = bb.get("bb_lower")
                            except Exception:
                                bb_lower = None
                            try:
                                bb_pos = bb.get("position_pct")
                                if bb_pos is None:
                                    bb_pos = bb.get("position")
                            except Exception:
                                bb_pos = None

                            indicators["bollinger"] = {
                                "upper": bb_upper,
                                "middle": bb_middle,
                                "lower": bb_lower,
                                "position_pct": bb_pos,
                            }

                            # Backfill Bollinger bands from df if analyzer only provides position/width.
                            try:
                                if indicators.get("bollinger"):
                                    if indicators["bollinger"].get("upper") is None:
                                        uv = _df_get("bb_upper")
                                        if uv is not None:
                                            indicators["bollinger"]["upper"] = float(uv)
                                    if indicators["bollinger"].get("middle") is None:
                                        mv = _df_get("bb_middle")
                                        if mv is not None:
                                            indicators["bollinger"]["middle"] = float(mv)
                                    if indicators["bollinger"].get("lower") is None:
                                        lv = _df_get("bb_lower")
                                        if lv is not None:
                                            indicators["bollinger"]["lower"] = float(lv)
                            except Exception:
                                pass
                        except Exception:
                            indicators["bollinger"] = {}
                        dp["indicators"] = indicators

                    # macro + headlines
                    if not isinstance(dp.get("macro"), dict) or not dp.get("macro"):
                        macro = {
                            "score": news_data.get("score"),
                            "dxy": news_data.get("dxy"),
                            "yields": news_data.get("yields"),
                            "vix": news_data.get("vix"),
                            "sentiment": news_data.get("sentiment"),
                            "high_impact_news_soon": news_data.get("high_impact_news_soon"),
                            "geopolitical_risk": news_data.get("geopolitical_risk"),
                            "anomalies": news_data.get("anomalies"),
                            "calendar": calendar_data,
                        }
                        dp["macro"] = macro

                    # ml_predictions (AgentTools.get_ml_prediction expects ml/ml_predictions)
                    if isinstance(ml_data, dict) and ml_data:
                        if not isinstance(dp.get("ml_predictions"), dict) or not dp.get("ml_predictions"):
                            dp["ml_predictions"] = ml_data
                        if not isinstance(dp.get("ml"), dict) or not dp.get("ml"):
                            dp["ml"] = ml_data

                    if not isinstance(dp.get("headlines"), list) or not dp.get("headlines"):
                        # Try common shapes from news provider.
                        headlines = None
                        try:
                            headlines = news_data.get("headlines")
                        except Exception:
                            headlines = None
                        if headlines is None:
                            try:
                                headlines = news_data.get("news_headlines")
                            except Exception:
                                headlines = None
                        if headlines is None:
                            try:
                                sentiment = news_data.get("sentiment") if isinstance(news_data.get("sentiment"), dict) else {}
                                headlines = sentiment.get("headlines")
                            except Exception:
                                headlines = None
                        if headlines is None:
                            try:
                                # get_news_detailed often nests analyzed headlines under sentiment.headlines (dicts with title)
                                sentiment = news_data.get("sentiment") if isinstance(news_data.get("sentiment"), dict) else {}
                                headlines = sentiment.get("news_headlines")
                            except Exception:
                                headlines = None
                        # Hybrid news detailed output does not include headline text; attempt alternate keys if present.
                        if headlines is None:
                            try:
                                components = news_data.get("components") if isinstance(news_data.get("components"), dict) else {}
                                hcomp = components.get("headlines") if isinstance(components.get("headlines"), dict) else {}
                                headlines = hcomp.get("headlines")
                            except Exception:
                                headlines = None
                        if isinstance(headlines, list):
                            cleaned = []
                            for h in headlines:
                                try:
                                    if isinstance(h, dict):
                                        txt = h.get("title") or h.get("headline") or h.get("text")
                                    else:
                                        txt = h
                                    txt = str(txt).strip() if txt is not None else ""
                                    if txt:
                                        cleaned.append(txt)
                                except Exception:
                                    continue
                            cleaned = cleaned[:10]
                            dp["headlines"] = cleaned
                            dp["news_headlines"] = cleaned
                        else:
                            try:
                                # Debug visibility only when missing; do not spam INFO.
                                keys_preview = list(news_data.keys())[:25] if isinstance(news_data, dict) else []
                                log.debug(f"AGENT_CACHE | headlines missing | news_data keys={keys_preview}")
                            except Exception:
                                pass

                    # calendar
                    if not isinstance(dp.get("calendar"), dict) or not dp.get("calendar"):
                        if isinstance(calendar_data, dict) and calendar_data:
                            dp["calendar"] = calendar_data

                    # sr_zones: use cached zones already computed this cycle
                    if not isinstance(dp.get("sr_zones"), list) or not dp.get("sr_zones"):
                        sr_zones = getattr(self, "_last_sr_zones", None)
                        if isinstance(sr_zones, list) and sr_zones:
                            zones_out = []
                            for z in sr_zones:
                                try:
                                    zones_out.append(
                                        {
                                            "timeframe": getattr(z, "timeframe", None),
                                            "zone_type": getattr(z, "zone_type", None),
                                            "price_low": getattr(z, "price_low", None),
                                            "price_high": getattr(z, "price_high", None),
                                            "midpoint": getattr(z, "midpoint", None),
                                            "touches": getattr(z, "touches", None),
                                            "strength": getattr(z, "strength", None),
                                            "confluence": getattr(z, "confluence", None),
                                            "age_bars": getattr(z, "age_bars", None),
                                        }
                                    )
                                except Exception:
                                    continue
                            if zones_out:
                                dp["sr_zones"] = zones_out

                    # candles (for AgentTools.get_candles cache path)
                    existing_candles = dp.get("candles") if isinstance(dp.get("candles"), dict) else {}
                    candles_cache = existing_candles.copy() if isinstance(existing_candles, dict) else {}

                    # M5/H1/H4/D1 from MT5 (backfill individual TFs)
                    try:
                        import MetaTrader5 as mt5

                        def _rates_to_candles(rates):
                            out = []
                            if rates is None:
                                return out
                            for r in rates:
                                try:
                                    tv = 0.0
                                    try:
                                        tv = float(r["tick_volume"])
                                    except Exception:
                                        try:
                                            tv = float(r["real_volume"])
                                        except Exception:
                                            tv = 0.0
                                    out.append(
                                        {
                                            "time": datetime.utcfromtimestamp(int(r["time"]) - _mt5_server_offset()).isoformat(),
                                            "open": float(r["open"]),
                                            "high": float(r["high"]),
                                            "low": float(r["low"]),
                                            "close": float(r["close"]),
                                            "volume": tv,
                                        }
                                    )
                                except Exception:
                                    continue
                            return out

                        try:
                            have_m5 = isinstance(candles_cache.get("M5"), list) and bool(candles_cache.get("M5"))
                        except Exception:
                            have_m5 = False
                        if not have_m5:
                            m5_rates = mt5.copy_rates_from_pos(config.SYMBOL, mt5.TIMEFRAME_M5, 0, 20)
                            m5_list = _rates_to_candles(m5_rates)
                            if m5_list:
                                candles_cache["M5"] = m5_list

                        # FLO-221: Fetch M15 candles for multi-TF indicators
                        try:
                            have_m15 = isinstance(candles_cache.get("M15"), list) and bool(candles_cache.get("M15"))
                        except Exception:
                            have_m15 = False
                        if not have_m15:
                            _m15_t0 = time.time()
                            m15_rates = mt5.copy_rates_from_pos(config.SYMBOL, mt5.TIMEFRAME_M15, 0, 250)
                            m15_list = _rates_to_candles(m15_rates)
                            if m15_list:
                                candles_cache["M15"] = m15_list
                            log.debug(f"MT5_FETCH | M15 candles: {len(m15_list) if m15_list else 0} bars in {(time.time() - _m15_t0)*1000:.0f}ms")

                        try:
                            have_h1 = isinstance(candles_cache.get("H1"), list) and bool(candles_cache.get("H1"))
                        except Exception:
                            have_h1 = False
                        if not have_h1:
                            h1_rates = mt5.copy_rates_from_pos(config.SYMBOL, mt5.TIMEFRAME_H1, 0, 250)
                            h1_list = _rates_to_candles(h1_rates)
                            if h1_list:
                                candles_cache["H1"] = h1_list

                        try:
                            have_h4 = isinstance(candles_cache.get("H4"), list) and bool(candles_cache.get("H4"))
                        except Exception:
                            have_h4 = False
                        if not have_h4:
                            h4_rates = mt5.copy_rates_from_pos(config.SYMBOL, mt5.TIMEFRAME_H4, 0, 250)
                            h4_list = _rates_to_candles(h4_rates)
                            if h4_list:
                                candles_cache["H4"] = h4_list

                        try:
                            have_d1 = isinstance(candles_cache.get("D1"), list) and bool(candles_cache.get("D1"))
                        except Exception:
                            have_d1 = False
                        if not have_d1:
                            d1_rates = mt5.copy_rates_from_pos(config.SYMBOL, mt5.TIMEFRAME_D1, 0, 250)
                            d1_list = _rates_to_candles(d1_rates)
                            if d1_list:
                                candles_cache["D1"] = d1_list
                    except Exception:
                        pass

                    if candles_cache:
                        try:
                            tgt = dp.setdefault("candles", {})
                            if not isinstance(tgt, dict):
                                tgt = {}
                                dp["candles"] = tgt
                            # Merge to avoid overwriting other TFs populated elsewhere.
                            tgt.update(candles_cache)
                        except Exception:
                            # Fallback to replacement if merge fails for any reason.
                            try:
                                dp["candles"] = candles_cache
                            except Exception:
                                pass

                    # FLO-221: Compute multi-TF indicators from cached candles
                    try:
                        from technical_analyzer import compute_indicators_from_candles
                        _mtf_t0 = time.time()
                        multi_tf = {}
                        _all_candles = dp.get("candles") or {}
                        for _tf in ["M15", "H1", "H4", "D1"]:
                            _tf_candles = _all_candles.get(_tf)
                            if isinstance(_tf_candles, list) and len(_tf_candles) >= 14:
                                multi_tf[_tf] = compute_indicators_from_candles(_tf_candles)
                        dp["multi_tf_indicators"] = multi_tf
                        log.debug(f"MULTI_TF_INDICATORS | {list(multi_tf.keys())} computed in {(time.time() - _mtf_t0)*1000:.0f}ms")
                    except Exception as e_mtf:
                        log.warning(f"MULTI_TF_INDICATORS | Error: {e_mtf}")
                        dp["multi_tf_indicators"] = {}

                    # FLO-223: 3-layer Pivot Points (Daily + Weekly + Monthly)
                    try:
                        def _compute_pivots(candle):
                            H, L, C = float(candle["high"]), float(candle["low"]), float(candle["close"])
                            P = (H + L + C) / 3.0
                            rng = H - L
                            return {
                                "classic": {
                                    "R3": round(H + 2 * (P - L), 2), "R2": round(P + rng, 2),
                                    "R1": round(2 * P - L, 2), "PP": round(P, 2),
                                    "S1": round(2 * P - H, 2), "S2": round(P - rng, 2),
                                    "S3": round(L - 2 * (H - P), 2),
                                },
                                "fibonacci": {
                                    "R3": round(P + rng, 2), "R2": round(P + 0.618 * rng, 2),
                                    "R1": round(P + 0.382 * rng, 2), "PP": round(P, 2),
                                    "S1": round(P - 0.382 * rng, 2), "S2": round(P - 0.618 * rng, 2),
                                    "S3": round(P - rng, 2),
                                },
                                "source": {"date": candle.get("time"), "high": H, "low": L, "close": C},
                            }

                        _all_candles = dp.get("candles") or {}
                        _pivots = {}

                        # Daily from D1[-2]
                        _d1c = _all_candles.get("D1")
                        if isinstance(_d1c, list) and len(_d1c) >= 2:
                            _pivots["daily"] = _compute_pivots(_d1c[-2])

                        # Weekly from W1[-2]
                        try:
                            w1_rates = mt5.copy_rates_from_pos(config.SYMBOL, mt5.TIMEFRAME_W1, 0, 3)
                            w1_list = _rates_to_candles(w1_rates)
                            if isinstance(w1_list, list) and len(w1_list) >= 2:
                                _pivots["weekly"] = _compute_pivots(w1_list[-2])
                        except Exception:
                            pass

                        # Monthly from MN1[-2]
                        try:
                            mn1_rates = mt5.copy_rates_from_pos(config.SYMBOL, mt5.TIMEFRAME_MN1, 0, 3)
                            mn1_list = _rates_to_candles(mn1_rates)
                            if isinstance(mn1_list, list) and len(mn1_list) >= 2:
                                _pivots["monthly"] = _compute_pivots(mn1_list[-2])
                        except Exception:
                            pass

                        if _pivots:
                            dp["pivot_points"] = _pivots
                            _layers = list(_pivots.keys())
                            _dpp = _pivots.get("daily", {}).get("classic", {}).get("PP", "?")
                            log.debug(f"PIVOT_POINTS | layers={_layers} daily_PP={_dpp}")
                    except Exception as e_pp:
                        log.debug(f"PIVOT_POINTS | Error: {e_pp}")

                    # Snapshot candles separately for proactive Agent calls (avoid MT5 calls/reference issues)
                    try:
                        if isinstance(dp.get("candles"), dict):
                            self._cached_candles = dict(dp.get("candles") or {})
                    except Exception:
                        pass

                    # fibonacci (multi-timeframe retracement levels: H1/H4/D1)
                    if not isinstance(dp.get("fibonacci"), dict) or not dp.get("fibonacci"):
                        try:
                            def _compute_fib_from_high_low(swing_high_f: float, swing_low_f: float) -> dict:
                                rng_f = swing_high_f - swing_low_f
                                if rng_f <= 0:
                                    return {}
                                return {
                                    "swing_high": swing_high_f,
                                    "swing_low": swing_low_f,
                                    "levels": {
                                        "23.6": swing_high_f - (rng_f * 0.236),
                                        "38.2": swing_high_f - (rng_f * 0.382),
                                        "50.0": swing_high_f - (rng_f * 0.500),
                                        "61.8": swing_high_f - (rng_f * 0.618),
                                    },
                                }

                            fib_out = {}

                            # H1: last 20 candles from df
                            try:
                                lookback = 20
                                if len(df) >= 2:
                                    tail = df.tail(lookback)
                                    sh = float(tail["high"].max())
                                    sl = float(tail["low"].min())
                                    h1_fib = _compute_fib_from_high_low(sh, sl)
                                    if h1_fib:
                                        fib_out["H1"] = h1_fib
                            except Exception:
                                pass

                            # H4/D1: from cached candles in dp["candles"]
                            try:
                                candles_dp = dp.get("candles") if isinstance(dp.get("candles"), dict) else {}
                            except Exception:
                                candles_dp = {}

                            def _tf_swing(candles_list):
                                if not isinstance(candles_list, list) or len(candles_list) < 2:
                                    return None
                                hi = None
                                lo = None
                                for c in candles_list:
                                    if not isinstance(c, dict):
                                        continue
                                    try:
                                        ch = c.get("high") if c.get("high") is not None else c.get("h")
                                        cl = c.get("low") if c.get("low") is not None else c.get("l")
                                        if ch is None or cl is None:
                                            continue
                                        ch_f = float(ch)
                                        cl_f = float(cl)
                                    except Exception:
                                        continue
                                    if hi is None or ch_f > hi:
                                        hi = ch_f
                                    if lo is None or cl_f < lo:
                                        lo = cl_f
                                if hi is None or lo is None:
                                    return None
                                return hi, lo

                            try:
                                h4_swing = _tf_swing(candles_dp.get("H4"))
                                if h4_swing:
                                    h4_fib = _compute_fib_from_high_low(h4_swing[0], h4_swing[1])
                                    if h4_fib:
                                        fib_out["H4"] = h4_fib
                            except Exception:
                                pass

                            try:
                                d1_swing = _tf_swing(candles_dp.get("D1"))
                                if d1_swing:
                                    d1_fib = _compute_fib_from_high_low(d1_swing[0], d1_swing[1])
                                    if d1_fib:
                                        fib_out["D1"] = d1_fib
                            except Exception:
                                pass

                            if fib_out:
                                dp["fibonacci"] = fib_out
                        except Exception:
                            pass

                    # current_price: convert float to expected bid/ask dict shape
                    cp = dp.get("current_price")
                    if not isinstance(cp, dict):
                        try:
                            price_f = float(cp) if cp is not None else None
                        except Exception:
                            price_f = None
                        if price_f is not None:
                            dp["current_price"] = {
                                "bid": price_f,
                                "ask": price_f,
                                "spread": 0.0,
                                "timestamp": datetime.utcnow().isoformat(timespec="seconds") + "Z",
                            }
            except Exception:
                # Never block trading loop
                pass

            # ================================================================
            # PROACTIVE AI AGENT (H1 snapshot) — DISABLED
            # H1 boundary must not call Floki independently.
            # Agent controls its call frequency via set_next_check.
            # ================================================================

            # ================================================================
            # SCHEDULED AI AGENT (timer gate)
            # The analysis cycle runs every minute; the Agent is called only
            # when next_check_at is due (or missing/invalid).
            # ================================================================
            try:
                use_agent = bool(getattr(config, "USE_AI_AGENT", False))
                if use_agent:
                    base_dir = os.path.dirname(os.path.abspath(__file__))
                    next_path = os.path.join(base_dir, "data", "agent_next_check.json")

                    requested_minutes = None
                    next_check_at = None
                    try:
                        if os.path.exists(next_path):
                            with open(next_path, "r", encoding="utf-8") as f:
                                payload = json.load(f)
                            if isinstance(payload, dict):
                                requested_minutes = payload.get("requested_minutes")
                                next_check_at = payload.get("next_check_at")
                    except Exception:
                        requested_minutes = None
                        next_check_at = None

                    def _parse_next_check(iso_s: Optional[str]) -> Optional[datetime]:
                        if not isinstance(iso_s, str) or not iso_s.strip():
                            return None
                        s = iso_s.strip()
                        try:
                            if s.endswith("Z"):
                                s = s[:-1] + "+00:00"
                        except Exception:
                            pass
                        try:
                            dt = datetime.fromisoformat(s)
                        except Exception:
                            return None
                        try:
                            if dt.tzinfo is not None:
                                return dt.astimezone(timezone.utc).replace(tzinfo=None)
                        except Exception:
                            pass
                        return None

                    def _clamp_minutes(m: Any) -> int:
                        try:
                            m_i = int(m)
                        except Exception:
                            m_i = 5
                        if m_i < 2:
                            return 2
                        if m_i > 120:
                            return 120
                        return m_i

                    now_utc = datetime.utcnow()
                    scheduled_dt = _parse_next_check(next_check_at)
                    due = (scheduled_dt is None) or (scheduled_dt <= now_utc)

                    if due:
                        log.info("FLOKI_SCHEDULE | Calling Floki now (timer due)")
                        self.agent_proactive_out_of_cycle(
                            trigger_type="SCHEDULED",
                            trigger_data={
                                "due": True,
                                "next_check_at": next_check_at,
                                "requested_minutes": requested_minutes,
                            },
                        )
                    else:
                        delta_s = max(0, (scheduled_dt - now_utc).total_seconds())
                        approx_minutes = int(round(delta_s / 60.0))
                        sleep_minutes = _clamp_minutes(approx_minutes)
                        try:
                            milestones = {30, 25, 20, 15, 10, 5, 2, 1}
                            last_logged = getattr(self, "_last_floki_schedule_milestone", None)
                            if sleep_minutes in milestones and sleep_minutes != last_logged:
                                setattr(self, "_last_floki_schedule_milestone", sleep_minutes)
                                log.info(
                                    f"FLOKI_SCHEDULE | Next check in {sleep_minutes} minutes (agent requested — skipping Floki this cycle)"
                                )
                        except Exception:
                            pass
            except Exception as e:
                log.warning(f"FLOKI_SCHEDULE | schedule check error (ignored): {e}")
            
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
                try:
                    pa = last_analysis.get("proactive_analysis") if isinstance(last_analysis.get("proactive_analysis"), dict) else None
                    if pa:
                        pa_dec = pa.get("decision")
                        pa_conf = pa.get("confidence")
                        if pa_dec is not None:
                            last_analysis["agent_decision"] = pa_dec
                        if pa_conf is not None:
                            last_analysis["agent_confidence"] = pa_conf
                except Exception:
                    pass
            
            # Analysis log
            log.analysis(tech_score, news_score, ml_score, final_score)
            
            if config.USE_CENTRAL_BRAIN:
                self._check_heartbeat()

            # Check if actionable signal
            if direction is None:
                return
            
            # Signal detected!
            

            # alert_brain_decision disabled in Phase 0 (dashboard shows Brain decision)

            atr = get_atr_value(df)
            prices = executor.get_current_price()
            if prices:
                entry_price = prices[1] if direction == "BUY" else prices[0]
            else:
                entry_price = df['close'].iloc[-1]

            levels = calculate_sl_tp(entry_price, direction, atr)
            
            # alert_signal_detected disabled in Phase 0 (Brain no longer executes trades)
            
            # NOTE: Safety checks and execution must be reconnected under Agent execution (Phase 3)
            return

            
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
                log.info(f"   M5 Check: {m5_check['description']}")
                
                if m5_check["reversal_detected"]:
                    if m5_check["reversal_strength"] == "strong":
                        log.safety_block(f"Strong M5 reversal: {m5_check['description']}")
                        alert_m5_reversal_block(direction, m5_check["recent_move_pct"], m5_check["description"])
                        return
                    elif m5_check["reversal_strength"] == "moderate":
                        confidence -= config.M5_REVERSAL_CONFIDENCE_PENALTY
                        log.info(f"   Moderate M5 reversal: confidence reduced {config.M5_REVERSAL_CONFIDENCE_PENALTY} → {confidence:.1f}")
                        if confidence < config.BRAIN_MIN_CONFIDENCE:
                            log.safety_block(f"Moderate M5 reversal reduced confidence below minimum ({confidence:.1f} < {config.BRAIN_MIN_CONFIDENCE})")
                            alert_m5_reversal_block(direction, m5_check["recent_move_pct"], m5_check["description"])
                            return
            except Exception as e:
                log.warning(f"M5 reversal check error (ignored): {e}")
            
            # AI Agent Shadow Mode: Call Agent AFTER safety checks pass
            # This ensures Agent is not called when safety blocks the trade
            pass
            
            # Spread Check with Retry Loop
            spread = executor.get_spread()
            if spread is not None:
                log.info(f"   Spread: {spread:.1f} pips")
                
                if spread > config.MAX_SPREAD_PIPS:
                    log.warning(f"   Spread too high: {spread:.1f} pips (max: {config.MAX_SPREAD_PIPS}) — delaying entry")
                    alert_spread_delay(spread, config.MAX_SPREAD_PIPS, 1)
                    
                    # Retry loop
                    for retry in range(2, config.SPREAD_MAX_RETRIES + 1):
                        time.sleep(config.SPREAD_RETRY_INTERVAL_SECONDS)
                        spread = executor.get_spread()
                        
                        if spread is None:
                            log.warning(f"   Could not get spread on retry #{retry}")
                            continue
                        
                        log.info(f"   Spread retry #{retry}: {spread:.1f} pips")
                        
                        if spread <= config.MAX_SPREAD_PIPS:
                            log.info(f"   Spread normalized: {spread:.1f} pips — proceeding with entry")
                            break
                    else:
                        # Exhausted all retries
                        log.warning(f"   Spread did not normalize after {config.SPREAD_MAX_RETRIES} retries — trade skipped")
                        alert_spread_skip(direction, spread if spread else 0, final_score)
                        return
            else:
                log.warning("   Could not get spread — proceeding anyway")
            
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
            be_trigger, tr_trigger, tr_distance, max_drawdown_pips = get_ea_management_params(
                sl_pips=sl_pips_orig,
                volatility_status=getattr(self, "_last_vol_status", None),
            )
            
            # Check if EA bridge is enabled and EA is online
            use_ea = False
            if getattr(config, 'USE_EA_BRIDGE', False) and self.executes_trades:
                try:
                    from ea_bridge import is_ea_online, write_signal
                    stale_threshold = getattr(config, 'EA_STALE_THRESHOLD_SECONDS', 60)
                    if is_ea_online(stale_threshold):
                        use_ea = True
                        log.info(f"   EA Bridge: ONLINE — sending signal via JSON")
                    else:
                        log.warning(f"   EA Bridge: OFFLINE — falling back to direct MT5 API")
                except Exception as e:
                    log.warning(f"   EA Bridge error: {e} — falling back to direct MT5 API")
            
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
                    max_drawdown_pips=max_drawdown_pips,
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
                        decision_source="brain",
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
                        decision_source="brain",
                    )
                else:
                    log.error(f"Failed to execute trade: {order_result.error_message}")
        finally:
            # Check EA Bridge status and alert if offline (must never block the bot)
            check_ea_bridge_status_and_alert()
            # Persist state for dashboard (must never block the bot)
            write_state(self)
            # FLO-155: Periodic health check (every 60 min, never blocks)
            try:
                from health_check import maybe_run_health_check
                maybe_run_health_check()
            except Exception:
                pass

    def execute_agent_trade(
        self,
        direction: str,
        sl: float,
        tp: float,
        confidence: float,
        trade_plan: dict = None,
        scenario: str = "agent_proactive",
    ) -> dict:
        """Shared execution path for Agent-driven trade opens.

        This mirrors the (disabled) Brain execution pipeline, but uses Agent-provided SL/TP
        instead of ATR-derived levels.
        """
        try:
            direction = str(direction or "").upper()
            if direction not in ("BUY", "SELL"):
                reason = f"Invalid direction '{direction}'"
                log.warning(f"AGENT_EXEC | Reject: {reason}")
                try:
                    alert_safety_block(f"AGENT_{direction}", float(confidence or 0), reason)
                except Exception:
                    pass
                return {"success": False, "ticket": None, "reason": reason, "used_ea_bridge": False}

            try:
                sl_f = float(sl)
                tp_f = float(tp)
            except Exception:
                reason = "Invalid SL/TP types"
                log.warning(f"AGENT_EXEC | Reject: {reason} | sl={sl} tp={tp}")
                try:
                    alert_safety_block(f"AGENT_{direction}", float(confidence or 0), reason)
                except Exception:
                    pass
                return {"success": False, "ticket": None, "reason": reason, "used_ea_bridge": False}

            prices = None
            try:
                prices = executor.get_current_price()
            except Exception:
                prices = None
            if prices:
                entry_price = prices[1] if direction == "BUY" else prices[0]
            else:
                reason = "MT5 price unavailable"
                log.warning(f"AGENT_EXEC | Reject: {reason}")
                try:
                    alert_safety_block(f"AGENT_{direction}", float(confidence or 0), reason)
                except Exception:
                    pass
                return {"success": False, "ticket": None, "reason": reason, "used_ea_bridge": False}

            if direction == "BUY":
                if not (sl_f < entry_price and tp_f > entry_price):
                    reason = f"Invalid levels for BUY: entry={entry_price:.2f} sl={sl_f:.2f} tp={tp_f:.2f}"
                    log.warning(f"AGENT_EXEC | Reject: {reason}")
                    try:
                        alert_safety_block("AGENT_BUY", float(confidence or 0), reason)
                    except Exception:
                        pass
                    return {"success": False, "ticket": None, "reason": reason, "used_ea_bridge": False}
            else:
                if not (sl_f > entry_price and tp_f < entry_price):
                    reason = f"Invalid levels for SELL: entry={entry_price:.2f} sl={sl_f:.2f} tp={tp_f:.2f}"
                    log.warning(f"AGENT_EXEC | Reject: {reason}")
                    try:
                        alert_safety_block("AGENT_SELL", float(confidence or 0), reason)
                    except Exception:
                        pass
                    return {"success": False, "ticket": None, "reason": reason, "used_ea_bridge": False}

            sl_pips = abs(entry_price - sl_f) / 0.1
            min_sl_pips = getattr(config, "MIN_SL_PIPS", 150)
            max_sl_pips = getattr(config, "MAX_SL_PIPS", 800)
            if sl_pips < float(min_sl_pips):
                reason = f"SL too tight: {sl_pips:.0f} pips < min {min_sl_pips}"
                log.warning(f"AGENT_EXEC | Reject: {reason}")
                try:
                    alert_safety_block(f"AGENT_{direction}", float(confidence or 0), reason)
                except Exception:
                    pass
                return {"success": False, "ticket": None, "reason": reason, "used_ea_bridge": False}
            if sl_pips > float(max_sl_pips):
                reason = f"SL too wide: {sl_pips:.0f} pips > max {max_sl_pips}"
                log.warning(f"AGENT_EXEC | Reject: {reason}")
                try:
                    alert_safety_block(f"AGENT_{direction}", float(confidence or 0), reason)
                except Exception:
                    pass
                return {"success": False, "ticket": None, "reason": reason, "used_ea_bridge": False}

            # Safety Checks
            positions_list = get_positions() if self.executes_trades else []

            if positions_list:
                existing_ticket = getattr(positions_list[0], "ticket", None)
                log.info(
                    f"AGENT_EXEC | Skipping OPEN — position already exists (ticket #{existing_ticket})"
                )
                return {
                    "success": False,
                    "ticket": existing_ticket,
                    "reason": "position already exists",
                    "used_ea_bridge": False,
                }
            account_balance = get_account_balance() if self.executes_trades else config.CAPITAL_INICIAL
            open_positions = len(positions_list)
            mt5_connected = is_mt5_connected() if self.executes_trades else True

            is_safe, reasons = is_safe_to_trade(
                account_balance=account_balance,
                open_positions=open_positions,
                mt5_connected=mt5_connected,
                has_high_impact_news=False,
                trade_direction=direction,
                open_positions_list=positions_list,
            )
            if not is_safe:
                reason_str = "; ".join(reasons)
                log.safety_block(reason_str)
                try:
                    alert_safety_block(f"AGENT_{direction}", float(confidence or 0), reason_str)
                except Exception:
                    pass
                return {"success": False, "ticket": None, "reason": reason_str, "used_ea_bridge": False}

            # Spread Check with Retry Loop
            spread = None
            try:
                spread = executor.get_spread()
            except Exception:
                spread = None
            if spread is not None:
                log.info(f"   Spread: {spread:.1f} pips")

                if spread > config.MAX_SPREAD_PIPS:
                    log.warning(
                        f"   Spread too high: {spread:.1f} pips (max: {config.MAX_SPREAD_PIPS}) — delaying entry"
                    )
                    try:
                        alert_spread_delay(spread, config.MAX_SPREAD_PIPS, 1)
                    except Exception:
                        pass

                    for retry in range(2, config.SPREAD_MAX_RETRIES + 1):
                        time.sleep(config.SPREAD_RETRY_INTERVAL_SECONDS)
                        try:
                            spread = executor.get_spread()
                        except Exception:
                            spread = None
                        if spread is None:
                            log.warning(f"   Could not get spread on retry #{retry}")
                            continue
                        log.info(f"   Spread retry #{retry}: {spread:.1f} pips")
                        if spread <= config.MAX_SPREAD_PIPS:
                            log.info(f"   Spread normalized: {spread:.1f} pips — proceeding with entry")
                            break
                    else:
                        log.warning(
                            f"   Spread did not normalize after {config.SPREAD_MAX_RETRIES} retries — trade skipped"
                        )
                        try:
                            alert_spread_skip(direction, spread if spread else 0, float(confidence or 0))
                        except Exception:
                            pass
                        return {
                            "success": False,
                            "ticket": None,
                            "reason": "Spread did not normalize",
                            "used_ea_bridge": False,
                        }
            else:
                log.warning("   Could not get spread — proceeding anyway")

            pos_size = calculate_position_size(account_balance, config.RISK_PER_TRADE, sl_pips)
            log.info(f"   Entry: {entry_price:.2f}")
            log.info(f"   SL: {sl_f:.2f} ({sl_pips:.0f} pips)")
            tp_pips = abs(tp_f - entry_price) / 0.1
            log.info(f"   TP1: {tp_f:.2f} ({tp_pips:.0f} pips)")
            log.info(f"   Lot: {pos_size.lot_size}")

            comment = f"Agent-{scenario}-{direction}-{int(confidence or 0)}"

            be_trigger, tr_trigger, tr_distance, max_drawdown_pips = get_ea_management_params(
                sl_pips=sl_pips,
                volatility_status=getattr(self, "_last_vol_status", None),
            )

            used_ea = False
            if getattr(config, "USE_EA_BRIDGE", False) and self.executes_trades:
                try:
                    from ea_bridge import is_ea_online, write_signal
                    stale_threshold = getattr(config, "EA_STALE_THRESHOLD_SECONDS", 60)
                    if is_ea_online(stale_threshold):
                        used_ea = True
                        log.info("   EA Bridge: ONLINE — sending signal via JSON")
                    else:
                        log.warning("   EA Bridge: OFFLINE — falling back to direct MT5 API")
                except Exception as e:
                    log.warning(f"   EA Bridge error: {e} — falling back to direct MT5 API")

            if used_ea:
                from ea_bridge import write_signal

                signal_ok = write_signal(
                    signal=direction,
                    sl=sl_f,
                    tp=tp_f,
                    lot_size=pos_size.lot_size,
                    confidence=float(confidence or 0),
                    breakeven_trigger_pips=be_trigger,
                    trailing_trigger_pips=tr_trigger,
                    trailing_distance_pips=tr_distance,
                    max_drawdown_pips=max_drawdown_pips,
                    comment=comment,
                )

                if signal_ok:
                    log.success(f"Signal sent to EA: {direction}")
                    self.daily_stats["trades"] += 1
                    record_trade_opened(direction)
                    record_trade_open(
                        ticket=0,
                        direction=direction,
                        volume=pos_size.lot_size,
                        open_price=entry_price,
                        sl=sl_f,
                        tp=tp_f,
                        comment=comment,
                        decision_source="floki_agent",
                    )
                    return {"success": True, "ticket": 0, "reason": None, "used_ea_bridge": True}

                reason = "Failed to send signal to EA"
                log.error(reason)
                try:
                    alert_error("Agent Execution Failed", reason)
                except Exception:
                    pass
                return {"success": False, "ticket": None, "reason": reason, "used_ea_bridge": True}

            trade_confidence = float(confidence or 0)
            trade_scenario = scenario
            trade_risk_amount = pos_size.risk_amount if pos_size else None
            trade_risk_percent = config.RISK_PER_TRADE

            if direction == "BUY":
                order_result = execute_buy(
                    lot_size=pos_size.lot_size,
                    sl=sl_f,
                    tp=tp_f,
                    comment=comment,
                    confidence=trade_confidence,
                    scenario=trade_scenario,
                    risk_amount=trade_risk_amount,
                    risk_percent=trade_risk_percent,
                )
            else:
                order_result = execute_sell(
                    lot_size=pos_size.lot_size,
                    sl=sl_f,
                    tp=tp_f,
                    comment=comment,
                    confidence=trade_confidence,
                    scenario=trade_scenario,
                    risk_amount=trade_risk_amount,
                    risk_percent=trade_risk_percent,
                )

            if order_result.success:
                log.success(f"Trade executed! Ticket: {order_result.ticket}")
                self.daily_stats["trades"] += 1
                record_trade_opened(direction)
                record_trade_open(
                    ticket=order_result.ticket,
                    direction=direction,
                    volume=pos_size.lot_size,
                    open_price=order_result.price,
                    sl=sl_f,
                    tp=tp_f,
                    comment=comment,
                    decision_source="floki_agent",
                )
                return {"success": True, "ticket": order_result.ticket, "reason": None, "used_ea_bridge": False}

            reason = f"Failed to execute trade: {order_result.error_message}"
            log.error(reason)
            try:
                alert_error("Agent Execution Failed", reason)
            except Exception:
                pass
            return {"success": False, "ticket": None, "reason": reason, "used_ea_bridge": False}
        except Exception as e:
            reason = f"Exception in execute_agent_trade: {e}"
            log.error(reason)
            try:
                alert_error("Agent Execution Error", str(e))
            except Exception:
                pass
            return {"success": False, "ticket": None, "reason": reason, "used_ea_bridge": False}
        finally:
            try:
                check_ea_bridge_status_and_alert()
            except Exception:
                pass
            try:
                write_state(self)
            except Exception:
                pass

    def close_agent_trade(self, ticket: int, reason: str) -> dict:
        """Close an open position by ticket.

        Uses direct MT5 API close via executor. Records a pending close entry and alerts Discord.
        """
        try:
            t = int(ticket)
        except Exception:
            t = 0

        if t <= 0:
            msg = f"Invalid ticket '{ticket}'"
            log.warning(f"AGENT_CLOSE | Reject: {msg}")
            try:
                alert_error("Agent Close Rejected", msg)
            except Exception:
                pass
            return {"success": False, "ticket": None, "reason": msg}

        reason_str = str(reason or "agent_close")

        try:
            order_result = close_position(t)
        except Exception as e:
            msg = f"close_position exception: {e}"
            log.error(f"AGENT_CLOSE | Failed: {msg}")
            try:
                alert_error("Agent Close Failed", msg)
            except Exception:
                pass
            return {"success": False, "ticket": t, "reason": msg}

        if not order_result or not getattr(order_result, "success", False):
            msg = getattr(order_result, "error_message", None) or "close failed"
            log.error(f"AGENT_CLOSE | Failed: ticket={t} | {msg}")
            try:
                alert_error("Agent Close Failed", f"ticket={t} | {msg}")
            except Exception:
                pass
            return {"success": False, "ticket": t, "reason": msg}

        # FLO-220: Try to get BE state from monitor if available
        _be_state = False
        try:
            if hasattr(self, '_position_monitor') and self._position_monitor:
                _be_state = self._position_monitor.breakeven_activated_tickets.get(t, False)
        except Exception:
            _be_state = False
        try:
            record_trade_close(
                ticket=t,
                close_price=getattr(order_result, "price", None),
                profit=None,
                close_reason=f"{reason_str} (pending)",
                close_time=utc_iso(),  # FLO-286
                breakeven_activated=_be_state,
            )
        except Exception:
            pass

        try:
            add_closed_trade(self, {
                "ticket": t,
                "direction": None,
                "volume": getattr(order_result, "volume", None),
                "open_price": None,
                "close_price": getattr(order_result, "price", None),
                "profit": None,
                "reason": reason_str,
                "close_time": utc_iso(),  # FLO-286
                "close_type": "agent",
                "estimated": False,
                "pending": True,
                "outcome": None,
                "orig_tp": None,
                "orig_sl": None,
            })
        except Exception:
            pass

        try:
            from discord_cards import build_floki_close_card, send_built_card
            send_built_card(build_floki_close_card(
                ticket=t, direction="CLOSE", pnl=0, close_reason=reason_str,
            ))
        except Exception:
            pass

        log.info(f"AGENT_CLOSE | Success: ticket={t} | reason={reason_str}")
        return {"success": True, "ticket": t, "reason": reason_str}

    def adjust_agent_trade(
        self,
        ticket: int,
        new_sl: Optional[float],
        new_tp: Optional[float],
        reason: str,
    ) -> dict:
        """Modify SL/TP for an open position.

        Uses direct MT5 API modify via executor.modify_position().
        Validates new levels against current price and direction.
        """
        try:
            t = int(ticket)
        except Exception:
            t = 0

        if t <= 0:
            msg = f"Invalid ticket '{ticket}'"
            log.warning(f"AGENT_ADJUST | Reject: {msg}")
            try:
                alert_error("Agent Adjust Rejected", msg)
            except Exception:
                pass
            return {"success": False, "ticket": None, "reason": msg}

        reason_str = str(reason or "agent_adjust")

        positions_list = []
        try:
            positions_list = get_positions() if self.executes_trades else []
        except Exception:
            positions_list = []

        pos = None
        for p in positions_list:
            if getattr(p, "ticket", None) == t:
                pos = p
                break

        if pos is None:
            msg = f"Position {t} not found"
            log.warning(f"AGENT_ADJUST | Reject: {msg}")
            try:
                alert_error("Agent Adjust Rejected", msg)
            except Exception:
                pass
            return {"success": False, "ticket": t, "reason": msg}

        direction = str(getattr(pos, "direction", "") or "").upper()
        current_price = getattr(pos, "current_price", None)
        if direction not in ("BUY", "SELL") or current_price is None:
            msg = f"Invalid position data for ticket {t}"
            log.warning(f"AGENT_ADJUST | Reject: {msg}")
            try:
                alert_error("Agent Adjust Rejected", msg)
            except Exception:
                pass
            return {"success": False, "ticket": t, "reason": msg}

        sl_f = None
        tp_f = None
        try:
            sl_f = float(new_sl) if new_sl is not None else None
        except Exception:
            sl_f = None
        try:
            tp_f = float(new_tp) if new_tp is not None else None
        except Exception:
            tp_f = None

        if sl_f is None and tp_f is None:
            msg = "No new SL/TP provided"
            log.warning(f"AGENT_ADJUST | Skip: ticket={t} | {msg}")
            return {"success": False, "ticket": t, "reason": msg}

        if direction == "SELL":
            if sl_f is not None and not (sl_f > float(current_price)):
                msg = f"Invalid SL for SELL: sl={sl_f:.2f} must be above price={float(current_price):.2f}"
                log.warning(f"AGENT_ADJUST | Reject: {msg}")
                try:
                    alert_error("Agent Adjust Rejected", msg)
                except Exception:
                    pass
                return {"success": False, "ticket": t, "reason": msg}
            if tp_f is not None and not (tp_f < float(current_price)):
                msg = f"Invalid TP for SELL: tp={tp_f:.2f} must be below price={float(current_price):.2f}"
                log.warning(f"AGENT_ADJUST | Reject: {msg}")
                try:
                    alert_error("Agent Adjust Rejected", msg)
                except Exception:
                    pass
                return {"success": False, "ticket": t, "reason": msg}
        else:
            if sl_f is not None and not (sl_f < float(current_price)):
                msg = f"Invalid SL for BUY: sl={sl_f:.2f} must be below price={float(current_price):.2f}"
                log.warning(f"AGENT_ADJUST | Reject: {msg}")
                try:
                    alert_error("Agent Adjust Rejected", msg)
                except Exception:
                    pass
                return {"success": False, "ticket": t, "reason": msg}
            if tp_f is not None and not (tp_f > float(current_price)):
                msg = f"Invalid TP for BUY: tp={tp_f:.2f} must be above price={float(current_price):.2f}"
                log.warning(f"AGENT_ADJUST | Reject: {msg}")
                try:
                    alert_error("Agent Adjust Rejected", msg)
                except Exception:
                    pass
                return {"success": False, "ticket": t, "reason": msg}

        reason_str = str(reason or "agent_adjust")

        try:
            order_result = executor.modify_position(t, new_sl=sl_f, new_tp=tp_f)
        except Exception as e:
            msg = f"modify_position exception: {e}"
            log.error(f"AGENT_ADJUST | Failed: {msg}")
            try:
                alert_error("Agent Adjust Failed", msg)
            except Exception:
                pass
            return {"success": False, "ticket": t, "reason": msg}

        if not order_result or not getattr(order_result, "success", False):
            msg = getattr(order_result, "error_message", None) or "modify failed"
            log.error(f"AGENT_ADJUST | Failed: ticket={t} | {msg}")
            try:
                alert_error("Agent Adjust Failed", f"ticket={t} | {msg}")
            except Exception:
                pass
            return {"success": False, "ticket": t, "reason": msg}

        try:
            from discord_cards import send_card, COLORS
            send_card("floki", COLORS["floki"], "\U0001F415 FLOKI \u2014 ADJUST",
                      f"ADJUST #{t} \u2014 SL ${sl_f:,.2f} TP ${tp_f:,.2f}",
                      fields=[
                          {"name": "Ticket", "value": str(t), "inline": True},
                          {"name": "New SL", "value": f"${sl_f:,.2f}", "inline": True},
                          {"name": "New TP", "value": f"${tp_f:,.2f}", "inline": True},
                          {"name": "Reason", "value": reason_str[:200], "inline": False},
                      ])
        except Exception:
            pass

        log.info(f"AGENT_ADJUST | Success: ticket={t} | SL={sl_f} TP={tp_f} | reason={reason_str}")
        return {"success": True, "ticket": t, "reason": reason_str}

    def agent_fast_decide(self, trigger_type: str, trigger_data: dict) -> dict:
        acquired = False
        try:
            try:
                acquired = self._fast_decision_lock.acquire(blocking=False)
            except Exception:
                acquired = True

            if not acquired:
                log.debug(f"AGENT_FAST | skip (in progress) | {trigger_type}")
                return {"success": False, "reason": "fast_decision_in_progress"}

            trigger_type = str(trigger_type or "")
            trigger_data = trigger_data if isinstance(trigger_data, dict) else {}

            prices = None
            try:
                prices = executor.get_current_price()
            except Exception:
                prices = None

            bid = None
            ask = None
            spread = None
            if prices:
                try:
                    bid = float(prices[0])
                    ask = float(prices[1])
                    spread = (ask - bid) / 0.1
                except Exception:
                    bid = None
                    ask = None
                    spread = None

            m5_candles = []
            try:
                import MetaTrader5 as mt5
                rates = mt5.copy_rates_from_pos(config.SYMBOL, mt5.TIMEFRAME_M5, 0, 10)
                if rates is not None:
                    for r in rates:
                        m5_candles.append({
                            "time": datetime.utcfromtimestamp(int(r["time"]) - _mt5_server_offset()).isoformat(),
                            "o": float(r["open"]),
                            "h": float(r["high"]),
                            "l": float(r["low"]),
                            "c": float(r["close"]),
                            "v": int(r["tick_volume"]),
                        })
            except Exception:
                m5_candles = []

            positions = []
            try:
                if self.executes_trades:
                    pos_list = executor.get_open_positions()

                    phase_by_ticket = {}
                    sl_by_ticket = {}
                    try:
                        from ea_bridge import read_ea_status

                        status = read_ea_status(stale_threshold_seconds=120)
                        if status and getattr(status, "positions", None):
                            for pos in status.positions:
                                try:
                                    t = int(getattr(pos, "ticket", 0) or 0)
                                    if t:
                                        phase_by_ticket[t] = getattr(pos, "phase", None)
                                        sl_by_ticket[t] = getattr(pos, "sl", None)
                                except Exception:
                                    continue
                    except Exception:
                        phase_by_ticket = {}
                        sl_by_ticket = {}

                    for p in pos_list[:5]:
                        p_sl = p.sl
                        try:
                            if int(p.ticket) in sl_by_ticket and sl_by_ticket.get(int(p.ticket)) is not None:
                                p_sl = float(sl_by_ticket.get(int(p.ticket)))
                        except Exception:
                            p_sl = p.sl

                        positions.append({
                            "ticket": p.ticket,
                            "direction": p.direction,
                            "open_price": p.open_price,
                            "current_price": p.current_price,
                            "sl": p_sl,
                            "tp": p.tp,
                            "profit": p.profit,
                            "profit_pips": p.profit_pips,
                            "phase": phase_by_ticket.get(int(p.ticket)) if phase_by_ticket else None,
                        })
            except Exception:
                positions = []

            upcoming_events = []
            try:
                from economic_calendar import get_upcoming_events
                upcoming_events = get_upcoming_events(max_events=3)
            except Exception:
                upcoming_events = []

            def _fast_note(action: str, ticket: Optional[int] = None, details: str = ""):
                try:
                    ttxt = f" ticket={ticket}" if ticket is not None else ""
                    log.info(f"AGENT_FAST | {action}{ttxt} | {details}")
                except Exception:
                    pass

                try:
                    # Persist for proactive agent visibility (monitor feed)
                    self._append_agent_monitor_event(
                        event=f"FAST_{action}",
                        ticket=ticket,
                        details=details or "",
                    )
                except Exception:
                    pass

                try:
                    # Persist to session memory so AgentTools.read_session_memory can surface it
                    import json
                    import os

                    base_dir = os.path.dirname(os.path.abspath(__file__))
                    data_dir = os.path.join(base_dir, "data")
                    os.makedirs(data_dir, exist_ok=True)
                    mem_path = os.path.join(data_dir, "agent_session_memory.json")

                    # FLO-309 regression fix: was datetime.now() → local time
                    # stored as session_date + stamped on last_updated fields.
                    today = trading_day_utc()
                    msg = f"FAST_AGENT {action}{ttxt}. {details}".strip()

                    payload = {
                        "session_date": today,
                        "thesis": "",
                        "trades_today": 0,
                        "wins_today": 0,
                        "losses_today": 0,
                        "notes": [],
                        "last_updated": utc_iso(),
                    }

                    if os.path.exists(mem_path):
                        try:
                            with open(mem_path, "r", encoding="utf-8") as f:
                                existing = json.load(f)
                            if isinstance(existing, dict):
                                payload.update(existing)
                        except Exception:
                            pass

                    if str(payload.get("session_date") or "") != today:
                        preserved_sage_notes = []
                        try:
                            for n in payload.get("notes") or []:
                                if isinstance(n, dict) and str(n.get("source") or "").strip().lower() == "sage":
                                    preserved_sage_notes.append(n)
                        except Exception:
                            preserved_sage_notes = []
                        payload = {
                            "session_date": today,
                            "thesis": "",
                            "trades_today": 0,
                            "wins_today": 0,
                            "losses_today": 0,
                            "notes": preserved_sage_notes,
                            "last_updated": utc_iso(),  # FLO-309 regression fix
                        }

                    if not isinstance(payload.get("notes"), list):
                        payload["notes"] = []
                    if msg:
                        payload["notes"].append({"time": utc_now().strftime("%H:%M"), "note": msg})  # FLO-309

                        # Keep max 20 notes, protect Sage notes from truncation.
                        # Strategy: keep all notes where source == 'sage', truncate only non-sage notes to last 19.
                        try:
                            all_notes = payload.get("notes") or []
                            sage_notes = []
                            normal_notes = []
                            for n in all_notes:
                                if isinstance(n, dict) and str(n.get("source") or "").strip().lower() == "sage":
                                    sage_notes.append(n)
                                else:
                                    normal_notes.append(n)
                            normal_notes = normal_notes[-19:]
                            payload["notes"] = normal_notes + sage_notes
                            payload["notes"] = payload["notes"][-20:]
                        except Exception:
                            payload["notes"] = payload["notes"][-20:]
                    payload["last_updated"] = utc_iso()  # FLO-309 regression fix

                    with open(mem_path, "w", encoding="utf-8") as f:
                        json.dump(payload, f, ensure_ascii=False, indent=2)
                except Exception:
                    pass

            price_payload = {"bid": bid, "ask": ask, "spread": spread}

            atr_points = 10.0
            try:
                la = self.last_analysis if isinstance(self.last_analysis, dict) else {}
                if isinstance(la.get("atr"), (int, float)):
                    atr_points = float(la.get("atr"))
                else:
                    ind = la.get("indicators") if isinstance(la.get("indicators"), dict) else {}
                    if isinstance(ind.get("atr_14"), (int, float)):
                        atr_points = float(ind.get("atr_14"))
                    else:
                        atr_block = ind.get("atr") if isinstance(ind.get("atr"), dict) else {}
                        if isinstance(atr_block.get("atr_value"), (int, float)):
                            atr_points = float(atr_block.get("atr_value"))
                        elif isinstance(atr_block.get("atr_current"), (int, float)):
                            atr_points = float(atr_block.get("atr_current"))
            except Exception:
                atr_points = 10.0

            try:
                from agent_data_builder import format_fast_xml
                user_message = format_fast_xml(
                    trigger_type=trigger_type,
                    trigger_data=trigger_data,
                    current_price=price_payload,
                    atr_points=atr_points,
                    m5_candles=m5_candles,
                    positions=positions,
                    upcoming_events=upcoming_events,
                )
            except Exception as e:
                log.debug(f"AGENT_FAST | XML build failed: {e}")
                return {"success": False, "reason": "format_fast_xml failed"}

            system_prompt = ""
            try:
                from agent_prompts import get_fast_system_prompt
                system_prompt = get_fast_system_prompt()
            except Exception:
                system_prompt = ""

            # Fast agent path: use the main tool-driven agent (Gemini) instead of legacy Anthropic.
            start_ts = time.time()
            model_used = None
            parsed = None
            try:
                import asyncio
                from ai_agent import get_agent, agent_decide
                from agent_tools import AgentTools
                import safety_checks
                import risk_manager

                agent = get_agent()
                if not agent.is_enabled():
                    return {"success": False, "reason": "agent_disabled"}

                tools_obj = AgentTools(
                    self,
                    executor=executor,
                    safety_checks_module=safety_checks,
                    risk_manager_module=risk_manager,
                )

                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                agent_result = loop.run_until_complete(
                    agent_decide(
                        user_message,
                        tools_obj,
                        trigger_type=f"FAST_{trigger_type}",
                    )
                )
                loop.close()

                model_used = getattr(agent_result, "model", None)
                # Map AgentResult into the legacy FAST parsed dict shape
                parsed = {
                    "action": "ACT" if agent_result.decision in ("OPEN_BUY", "OPEN_SELL", "CLOSE_TRADE", "ADJUST_TRADE") else "HOLD",
                    "reason": str(getattr(agent_result, "reasoning", "") or ""),
                    "execution": {},
                }
            except Exception as e:
                log.warning(f"AGENT_FAST | call failed (ignored): {e}")
                return {"success": False, "reason": str(e)}

            latency_ms = int((time.time() - start_ts) * 1000)
            input_tokens = 0
            output_tokens = 0

            action = None
            exec_payload = {}
            reason_txt = ""
            if isinstance(parsed, dict):
                action = str(parsed.get("action") or "").upper()
                exec_payload = parsed.get("execution") if isinstance(parsed.get("execution"), dict) else {}
                reason_txt = str(parsed.get("reason") or "")

            if action not in ("ACT", "HOLD", "DISMISS"):
                action = "HOLD"

            fast_state = {
                "timestamp": utc_iso(),  # FLO-286
                "trigger_type": trigger_type,
                "trigger_data": trigger_data,
                "action": action,
                "reason": reason_txt,
                "execution": exec_payload,
                "latency_ms": latency_ms,
                "model": model_used,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
            }
            try:
                if self.last_analysis and isinstance(self.last_analysis, dict):
                    self.last_analysis["fast_decision"] = fast_state
            except Exception:
                pass

            if action in ("HOLD", "DISMISS"):
                log.info(f"AGENT_FAST | {action} | {trigger_type} | {reason_txt}")
                return {"success": True, "action": action, "executed": False}

            exec_type = str(exec_payload.get("type") or "").upper()

            if exec_type == "OPEN":
                log.warning(f"AGENT_FAST | ACT blocked (OPEN disabled) | {trigger_type}")
                return {"success": True, "action": action, "executed": False, "reason": "fast_open_disabled"}

            if trigger_type.startswith("TRADE_RISK"):
                if exec_type not in ("CLOSE", "ADJUST"):
                    log.warning(f"AGENT_FAST | ACT blocked for risk trigger (type={exec_type})")
                    return {"success": True, "action": action, "executed": False, "reason": "risk trigger allows CLOSE/ADJUST only"}

            result = None
            if exec_type == "CLOSE":
                tickets = exec_payload.get("tickets")
                if not isinstance(tickets, list):
                    tickets = []
                if not tickets:
                    for p in positions:
                        t = p.get("ticket")
                        if t is not None:
                            tickets.append(t)
                for t in tickets:
                    try:
                        self.close_agent_trade(int(t), reason_txt or f"fast_{trigger_type}")
                    except Exception:
                        pass
                    try:
                        _fast_note(
                            "CLOSE",
                            ticket=int(t) if t is not None else None,
                            details=f"trigger={trigger_type} | reason={reason_txt}".strip(),
                        )
                    except Exception:
                        pass
                result = {"success": True, "closed": tickets}
            elif exec_type == "ADJUST":
                new_sl = exec_payload.get("new_sl")
                new_tp = exec_payload.get("new_tp")
                tickets = exec_payload.get("tickets")
                if not isinstance(tickets, list):
                    tickets = []
                if not tickets:
                    for p in positions:
                        t = p.get("ticket")
                        if t is not None:
                            tickets.append(t)
                for t in tickets:
                    try:
                        self.adjust_agent_trade(int(t), new_sl=new_sl, new_tp=new_tp, reason=reason_txt or f"fast_{trigger_type}")
                    except Exception:
                        pass
                    try:
                        _fast_note(
                            "ADJUST",
                            ticket=int(t) if t is not None else None,
                            details=f"trigger={trigger_type} | new_sl={new_sl} new_tp={new_tp} | reason={reason_txt}".strip(),
                        )
                    except Exception:
                        pass
                result = {"success": True, "adjusted": tickets}
            else:
                log.warning(f"AGENT_FAST | ACT but unknown execution type '{exec_type}'")
                result = {"success": False, "reason": f"unknown exec type {exec_type}"}

            log.info(f"AGENT_FAST | ACT | {trigger_type} | exec={exec_type} | result={result}")
            return {"success": True, "action": action, "executed": True, "result": result}
        except Exception as e:
            log.warning(f"AGENT_FAST | error (ignored): {e}")
            return {"success": False, "reason": str(e)}
        finally:
            if acquired:
                try:
                    self._fast_decision_lock.release()
                except Exception:
                    pass
        
        

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
        acquired = False
        try:
            log.info("PROACTIVE_H1 | disabled")
            return

            try:
                acquired = self._proactive_lock.acquire(blocking=False)
            except Exception:
                acquired = True

            if not acquired:
                log.info("PROACTIVE_H1 | skipped — analysis already running")
                return

            self._call_agent_proactive_snapshot(
                trigger_type="PROACTIVE_H1",
                snapshot_time_iso=h1_close_time_iso,
                agent_data=agent_data,
                df=df,
                trigger_data=None,
            )
        finally:
            if acquired:
                try:
                    self._proactive_lock.release()
                except Exception:
                    pass

    def _call_agent_proactive_snapshot(self, trigger_type: str, snapshot_time_iso: str, agent_data: dict, df, trigger_data: Optional[dict] = None):
        import asyncio
        from ai_agent import get_agent
        from agent_data_builder import get_session_name
        from db_writer import record_agent_proactive_analysis, get_recent_proactive_decisions
        from ai_agent import agent_decide
        from agent_tools import AgentTools
        import safety_checks
        import risk_manager

        base_dir = os.path.dirname(os.path.abspath(__file__))
        next_path = os.path.join(base_dir, "data", "agent_next_check.json")

        if trigger_type == "SIMBA_WAKE":
            try:
                now_ts = time.time()
                last_ts = getattr(self, "_last_simba_wake_call_ts", 0) or 0
                if (now_ts - last_ts) < 60:
                    log.info("SIMBA_WAKE | skipped — dedupe (<60s since last wake call)")
                    return
                self._last_simba_wake_call_ts = now_ts
            except Exception:
                pass

        if trigger_type in ("SIMBA_WAKE", "SIMBA_WATCH"):
            log.info("FLOKI_SCHEDULE | Simba override — calling Floki now (wake/watch condition met)")

        agent = get_agent()
        if not agent.is_enabled():
            return

        log.info(f"{trigger_type} | Calling AI Agent (tool-driven) | ts: {snapshot_time_iso}")

        try:
            # Ensure headlines exist for tool-driven investigation (non-blocking)
            try:
                dp = agent_data if isinstance(agent_data, dict) else None
                if dp is not None:
                    existing = dp.get("headlines") or dp.get("news_headlines")
                    have = isinstance(existing, list) and bool(existing)
                    if not have:
                        headlines_out = None
                        try:
                            from news_score_hybrid import get_hybrid_score_cached

                            cached = get_hybrid_score_cached()
                            result = cached.get("result", {})
                            comps = result.get("components", {}) if isinstance(result.get("components", {}), dict) else {}
                            raw = comps.get("headlines", {}).get("details", [])
                            if isinstance(raw, list) and raw:
                                titles = []
                                for h in raw[:10]:
                                    try:
                                        t = h.get("title") if isinstance(h, dict) else None
                                        if isinstance(t, str) and t.strip():
                                            titles.append(t.strip())
                                    except Exception:
                                        continue
                                if titles:
                                    headlines_out = titles
                        except Exception:
                            headlines_out = None

                        if isinstance(headlines_out, list) and headlines_out:
                            dp["headlines"] = headlines_out
                            dp["news_headlines"] = headlines_out
                        else:
                            try:
                                news_data = dp.get("news_data") if isinstance(dp.get("news_data"), dict) else {}
                                keys_preview = list(news_data.keys())[:25] if isinstance(news_data, dict) else []
                                log.debug(f"AGENT_CACHE | headlines missing (proactive) | news_data keys={keys_preview}")
                            except Exception:
                                pass
            except Exception:
                pass

            # Ensure AgentTools sees the enriched snapshot (it reads from self._last_agent_data)
            try:
                if isinstance(agent_data, dict):
                    self._last_agent_data = agent_data
            except Exception:
                pass

            trigger_context = f"{trigger_type} snapshot at {snapshot_time_iso}. Session: {get_session_name(datetime.utcnow().hour)}. "
            # FLO-321: for SIMBA_WAKE, render condition details in a readable
            # line so Floki doesn't have to re-investigate what "c2" was.
            # Fallback to the generic dict-dump for other trigger types or
            # when details are missing.
            if trigger_type == "SIMBA_WAKE" and isinstance(trigger_data, dict):
                _details = trigger_data.get("triggered_details") or []
                _cur = trigger_data.get("current_price")
                _cur_str = f"{_cur}" if _cur is not None else "n/a"
                if _details:
                    _parts = []
                    _had_desc = False
                    for _d in _details:
                        _desc = _d.get("description") or ""
                        if _desc: _had_desc = True
                        _parts.append(
                            f"condition {_d.get('id')} — {_d.get('type')} {_d.get('level')}"
                            + (f" ({_desc})" if _desc else "")
                        )
                    trigger_context += (
                        f"Simba wake: {'; '.join(_parts)} (current price: {_cur_str}). "
                    )
                    # FLO-331: the description text in parentheses was authored by
                    # Floki when he set the condition. FLO-321's passthrough echoes
                    # it back here verbatim — useful for context, but it can tighten
                    # a self-anchoring loop (e.g., reading your own "support test"
                    # framing as authoritative direction when the break may actually
                    # be a fake). Nudge Floki to re-verify against current data.
                    if _had_desc:
                        trigger_context += (
                            "(Descriptions above are YOUR prior interpretation when "
                            "setting the condition. Verify they still hold against "
                            "current market data — a broken level can flip S/R or "
                            "fail as a fake breakout. Check volume, momentum, and "
                            "regime before acting.) "
                        )
                elif trigger_data.get("expired"):
                    trigger_context += f"Simba wake: max_sleep expired (current price: {_cur_str}). "
                elif trigger_data.get("rex_critical"):
                    trigger_context += f"Simba wake: Rex monitor CRITICAL finding (current price: {_cur_str}). "
                else:
                    # Edge case — wake fired but we have no details at all
                    trigger_context += f"Simba wake fired (current price: {_cur_str}). Trigger data: {trigger_data}. "
            elif isinstance(trigger_data, dict) and trigger_data:
                trigger_context += f"Trigger data: {trigger_data}. "
            trigger_context += "Investigate using tools and respond with final decision JSON."

            # FLO-242: Condition confrontation REMOVED — Oracle at end (FLO-243) is the sole challenger.

            # FLO-179: previous_thesis injection removed to prevent confirmation bias.
            # Floki sees market data first. He can check his own notes via read_session_memory.
            # active_thesis.json is still WRITTEN after each decision (for dashboard + snapshots).

            # FLO-185: Inject objective deltas since last cycle (numbers only, no opinions)
            try:
                _snap_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "last_cycle_snapshot.json")
                if os.path.exists(_snap_path):
                    with open(_snap_path, "r", encoding="utf-8") as _sf:
                        _prev_snap = json.load(_sf)
                    if isinstance(_prev_snap, dict) and _prev_snap.get("price") is not None:
                        # Read current values
                        _cur_snap = {}
                        try:
                            _bs_path_d = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "bot_state.json")
                            with open(_bs_path_d, "r", encoding="utf-8") as _bsf:
                                _bs_d = json.load(_bsf)
                            _la_d = _bs_d.get("last_analysis", {})
                            _ind_d = _la_d.get("indicators", {})
                            _cur_snap["price"] = float(getattr(self, "last_known_price", 0) or 0) or None
                            _cur_snap["rsi"] = _ind_d.get("rsi_14")
                            _cur_snap["adx"] = _ind_d.get("adx_14")
                            _cur_snap["macd_hist"] = _ind_d.get("macd_hist")
                            _cur_snap["volume_ratio"] = _ind_d.get("volume_ratio")
                            _cur_snap["atr"] = _ind_d.get("atr_14")
                            _mr_d = _bs_d.get("market_regime", {})
                            _cur_snap["regime"] = _mr_d.get("regime")
                        except Exception:
                            pass

                        if _cur_snap.get("price") is not None:
                            # Calculate interval
                            _interval = ""
                            try:
                                _prev_ts = datetime.fromisoformat(_prev_snap["timestamp"].replace("Z", "+00:00"))
                                _elapsed_m = int((datetime.now(timezone.utc) - _prev_ts).total_seconds() / 60)
                                _interval = f' interval="{_elapsed_m}min"'
                            except Exception:
                                pass

                            def _delta(key, fmt=".1f"):
                                old = _prev_snap.get(key)
                                new = _cur_snap.get(key)
                                if old is None or new is None:
                                    return None
                                try:
                                    o = float(old); n = float(new); d = n - o
                                    sign = "+" if d >= 0 else ""
                                    return f"{o:{fmt}} -> {n:{fmt}} ({sign}{d:{fmt}})"
                                except Exception:
                                    return None

                            lines = []
                            _pd = _delta("price", ".0f")
                            if _pd:
                                # Add percentage
                                try:
                                    _pp = (float(_cur_snap["price"]) - float(_prev_snap["price"])) / float(_prev_snap["price"]) * 100
                                    _pd += f", {'+' if _pp >= 0 else ''}{_pp:.2f}%"
                                except Exception:
                                    pass
                                lines.append(f"PRICE: {_pd}")
                            _rd = _delta("rsi", ".1f")
                            if _rd: lines.append(f"RSI: {_rd}")
                            _ad = _delta("adx", ".1f")
                            if _ad: lines.append(f"ADX: {_ad}")
                            _md = _delta("macd_hist", ".2f")
                            if _md:
                                try:
                                    _mold = float(_prev_snap.get("macd_hist", 0))
                                    _mnew = float(_cur_snap.get("macd_hist", 0))
                                    if abs(_mnew) > abs(_mold):
                                        _md += " expanding"
                                    else:
                                        _md += " contracting"
                                except Exception:
                                    pass
                                lines.append(f"MACD_HIST: {_md}")

                            _vr = _cur_snap.get("volume_ratio")
                            if _vr is not None:
                                lines.append(f"VOLUME_RATIO: {_vr}")

                            # Regime change detection
                            _old_regime = _prev_snap.get("regime")
                            _new_regime = _cur_snap.get("regime")
                            if _old_regime and _new_regime:
                                if _old_regime != _new_regime:
                                    lines.append(f"REGIME: {_old_regime} -> {_new_regime} (CHANGED)")
                                else:
                                    lines.append(f"REGIME: {_new_regime} (unchanged)")

                            # Simba status
                            try:
                                _wc_path_d = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "agent_wake_conditions.json")
                                if os.path.exists(_wc_path_d):
                                    with open(_wc_path_d, "r", encoding="utf-8") as _wcf:
                                        _wc_d = json.load(_wcf)
                                    _wc_conds = _wc_d.get("conditions", [])
                                    if _wc_conds:
                                        _fired_ids = set(str(x) for x in _wc_d.get("fired_ids", []))
                                        _active_conds = [c for c in _wc_conds if str(c.get("id", "")) not in _fired_ids]
                                        _nearest = None
                                        _cprice = float(_cur_snap["price"])
                                        for _wcc in _active_conds:
                                            _lvl = _wcc.get("level")
                                            if _lvl is not None:
                                                _dist = abs(float(_lvl) - _cprice)
                                                if _nearest is None or _dist < _nearest[1]:
                                                    _nearest = (_wcc.get("type", "?"), _dist, float(_lvl))
                                        if _active_conds:
                                            _simba_str = f"SIMBA: {len(_active_conds)} conditions active"
                                            if _nearest:
                                                _simba_str += f", nearest: {_nearest[0]} {_nearest[2]:.0f} ({_nearest[1]:.0f} away)"
                                        elif _wc_conds:
                                            _simba_str = "SIMBA: All conditions have fired. Set new wake conditions."
                                        else:
                                            _simba_str = "SIMBA: No conditions set"
                                        lines.append(_simba_str)
                            except Exception:
                                pass

                            if lines:
                                trigger_context += f"\n<since_last_cycle{_interval}>\n" + "\n".join(lines) + "\n</since_last_cycle>\n"
            except Exception:
                pass

            # FLO-190: Rex Bull / Rex Bear structured debate (runs BEFORE Floki, injected into context)
            # FLO-203: SKIP debate+RM when position is open — RM is for ENTRY decisions only
            _debate_result = None
            _has_open_position = False
            try:
                _open_pos = executor.get_open_positions() or []
                _has_open_position = len(_open_pos) > 0
            except Exception:
                pass

            if not _has_open_position:
             try:
                from rex_validator import run_bull_bear_debate

                # Build data package from available caches
                _debate_data = {}
                try:
                    _debate_data["price"] = float(getattr(self, "last_known_price", 0) or 0) or None
                except Exception:
                    pass
                try:
                    _regime_ctx = getattr(self, "_last_regime_context", None)
                    if isinstance(_regime_ctx, dict):
                        _debate_data["regime"] = _regime_ctx.get("regime")
                except Exception:
                    pass
                try:
                    _bs_debate = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "bot_state.json")
                    with open(_bs_debate, "r", encoding="utf-8") as _bsf:
                        _bs_d2 = json.load(_bsf)
                    _debate_data["indicators"] = _bs_d2.get("last_analysis", {}).get("indicators", {})
                    # FLO-232: Inject multi-TF data so Rex Bear has D1/H4 bearish evidence
                    _mtf_d = _bs_d2.get("multi_tf_indicators", {})
                    _mtf_debate = {}
                    for _tf in ("M15", "H1", "H4", "D1"):
                        _tfd = _mtf_d.get(_tf, {})
                        if _tfd:
                            _mtf_debate[_tf] = {
                                "rsi": _tfd.get("rsi"),
                                "rsi_direction": _tfd.get("rsi_direction"),
                                "macd_direction": _tfd.get("macd_direction"),
                                "adx": _tfd.get("adx", {}).get("value"),
                                "ema_alignment": _tfd.get("ema_alignment"),
                                "price_vs_ema50": _tfd.get("price_vs_ema50"),
                                "price_vs_ema200": _tfd.get("price_vs_ema200"),
                            }
                    if _mtf_debate:
                        _debate_data["multi_tf"] = _mtf_debate
                except Exception:
                    pass
                try:
                    from luna_analyst import load_luna_brief
                    _lb = load_luna_brief()
                    if isinstance(_lb, dict):
                        _debate_data["luna"] = _lb
                except Exception:
                    pass
                # FLO-237: Replace raw Echo headlines with Luna's interpreted analysis
                # (prevents Rex Bull from inflating headline count)
                try:
                    _lb_debate = _debate_data.get("luna", {})
                    if isinstance(_lb_debate, dict) and _lb_debate.get("environment"):
                        _debate_data["news_context"] = {
                            "luna_environment": _lb_debate.get("environment"),
                            "luna_bias": _lb_debate.get("directional_bias"),
                            "luna_risk": _lb_debate.get("risk_level"),
                            "patterns": _lb_debate.get("patterns_detected", []),
                            "key_message": _lb_debate.get("key_message", ""),
                        }
                except Exception:
                    pass
                try:
                    from deep_search import load_deep_research
                    _dr_debate = load_deep_research()
                    if _dr_debate:
                        _debate_data["analyst_research"] = {
                            "consensus": _dr_debate.get("analyst_consensus"),
                            "key_insight": _dr_debate.get("key_insight"),
                            "risks": _dr_debate.get("risks_this_week", []),
                        }
                except Exception:
                    pass

                # FLO-211: Inject Rex monitor findings into debate context
                try:
                    from rex_monitor import load_rex_monitor
                    _rex_mon = load_rex_monitor()
                    if _rex_mon:
                        _ts = _rex_mon.get("timestamp")
                        _age = None
                        if _ts:
                            _st = datetime.fromisoformat(_ts.replace("Z", "+00:00"))
                            _age = round((datetime.now(timezone.utc) - _st).total_seconds() / 60, 1)
                        _debate_data["rex_monitor"] = {
                            "findings": _rex_mon.get("findings", []),
                            "alert_level": _rex_mon.get("alert_level", "QUIET"),
                            "age_minutes": _age,
                        }
                except Exception:
                    pass

                _debate_result = run_bull_bear_debate(_debate_data)

                if _debate_result and _debate_result.get("status") == "INJECTED":
                    _bull = _debate_result["rex_bull"]
                    _bear = _debate_result["rex_bear"]

                    # FLO-203: Research Manager v2 — reads 5 reports (Bull, Bear, Luna, Echo, Sage)
                    _verdict_result = None
                    try:
                        from research_manager import run_research_manager

                        # Gather Luna brief
                        _rm_luna = None
                        try:
                            _luna_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "luna_brief.json")
                            if os.path.exists(_luna_path):
                                with open(_luna_path, "r", encoding="utf-8") as _lf:
                                    _rm_luna = json.load(_lf)
                        except Exception:
                            pass

                        # FLO-237: Replace raw headline count with Luna's interpreted analysis
                        _rm_echo = None
                        try:
                            if _rm_luna and isinstance(_rm_luna, dict):
                                _rm_echo = {
                                    "luna_environment": _rm_luna.get("environment"),
                                    "luna_bias": _rm_luna.get("directional_bias"),
                                    "luna_risk": _rm_luna.get("risk_level"),
                                    "patterns": _rm_luna.get("patterns_detected", []),
                                }
                            # Add Deep Research if available
                            try:
                                from deep_search import load_deep_research
                                _dr_rm = load_deep_research()
                                if _dr_rm:
                                    if _rm_echo is None:
                                        _rm_echo = {}
                                    _rm_echo["analyst_consensus"] = _dr_rm.get("analyst_consensus")
                                    _rm_echo["analyst_insight"] = _dr_rm.get("key_insight", "")[:200]
                            except Exception:
                                pass
                        except Exception:
                            pass

                        # Gather Sage performance note from session memory
                        _rm_sage = None
                        try:
                            _sm_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "agent_session_memory.json")
                            if os.path.exists(_sm_path):
                                with open(_sm_path, "r", encoding="utf-8") as _smf:
                                    _sm = json.load(_smf)
                                _notes = _sm.get("notes", [])
                                for _n in reversed(_notes):
                                    if isinstance(_n, dict) and "sage" in str(_n.get("source", "")).lower():
                                        _rm_sage = str(_n.get("text", _n.get("content", "")))[:300]
                                        break
                                    elif isinstance(_n, str) and "sage" in _n.lower():
                                        _rm_sage = _n[:300]
                                        break
                        except Exception:
                            pass

                        # FLO-244: Build market snapshot for RM
                        _rm_snapshot = None
                        try:
                            _bs_rm_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "bot_state.json")
                            with open(_bs_rm_path, "r", encoding="utf-8") as _bsf_rm:
                                _bs_rm = json.load(_bsf_rm)
                            _rm_price = float(_bs_rm.get("last_known_price") or 0)
                            if _rm_price > 0:
                                _rm_regime = _bs_rm.get("market_regime", {}).get("regime", "")
                                _rm_change = _bs_rm.get("price_daily_change_pct")
                                _rm_adx = _bs_rm.get("last_analysis", {}).get("indicators", {}).get("adx_14")
                                _rm_dir = "DOWN" if (_rm_change or 0) < -0.1 else ("UP" if (_rm_change or 0) > 0.1 else "FLAT")

                                from agent_data_builder import get_session_name
                                _rm_session = get_session_name(datetime.utcnow().hour)

                                # Get S/R zones with roles
                                _rm_sups = []
                                _rm_ress = []
                                try:
                                    _sr_rm_path = config.SR_ZONES_JSON_PATH
                                    with open(_sr_rm_path, "r", encoding="utf-8") as _srf_rm:
                                        _sr_rm = json.load(_srf_rm)
                                    _sr_zones = _sr_rm if isinstance(_sr_rm, list) else _sr_rm.get("zones", [])
                                    for _z in _sr_zones:
                                        _zp = float(_z.get("price", 0))
                                        if not _zp:
                                            continue
                                        _zt = _z.get("zone_type", "")
                                        _zd = f"{_zt} {_z.get('timeframe', '')} {_z.get('touches', '')}t"
                                        if _zp < _rm_price:
                                            _entry = {"price": _zp, "detail": _zd, "dist": round(_rm_price - _zp, 1)}
                                            if str(_zt).upper() == "FLIP":
                                                _entry["flip_phase"] = "resistance \u2192 support"
                                            _rm_sups.append(_entry)
                                        elif _zp > _rm_price:
                                            _entry = {"price": _zp, "detail": _zd, "dist": round(_zp - _rm_price, 1)}
                                            if str(_zt).upper() == "FLIP":
                                                _entry["flip_phase"] = "support \u2192 resistance"
                                            _rm_ress.append(_entry)
                                    _rm_sups.sort(key=lambda x: x["dist"])
                                    _rm_ress.sort(key=lambda x: x["dist"])
                                except Exception:
                                    pass

                                # Direction-aware test type for nearest zone
                                _loc_note = ""
                                _nearest_all = sorted(_rm_sups + _rm_ress, key=lambda x: x["dist"])
                                if _nearest_all and _nearest_all[0]["dist"] < 5:
                                    _nz = _nearest_all[0]
                                    if _rm_dir == "DOWN":
                                        _loc_note = f"Price is FALLING toward {_nz['price']} ({_nz['dist']:.0f} pips) \u2014 SUPPORT TEST (may bounce)."
                                    elif _rm_dir == "UP":
                                        _loc_note = f"Price is RISING toward {_nz['price']} ({_nz['dist']:.0f} pips) \u2014 RESISTANCE TEST (may reject)."
                                    else:
                                        _loc_note = f"Price is FLAT near {_nz['price']} ({_nz['dist']:.0f} pips) \u2014 consolidating at level."
                                elif _rm_sups and _rm_sups[0]["dist"] < 15:
                                    _loc_note = f"Price is near support ({_rm_sups[0]['dist']:.0f} pips above)."
                                elif _rm_ress and _rm_ress[0]["dist"] < 15:
                                    _loc_note = f"Price is near resistance ({_rm_ress[0]['dist']:.0f} pips below)."
                                else:
                                    _loc_note = "Price is in the middle \u2014 no clear location edge, use momentum."

                                _rm_snapshot = {
                                    "price": _rm_price,
                                    "support_zones": _rm_sups[:3],
                                    "resistance_zones": _rm_ress[:3],
                                    "location_note": _loc_note,
                                    "direction": _rm_dir,
                                    "session": _rm_session,
                                    "regime": _rm_regime,
                                    "adx": _rm_adx,
                                }
                        except Exception:
                            pass

                        _verdict_result = run_research_manager(_bull, _bear, _rm_luna, _rm_echo, _rm_sage, market_snapshot=_rm_snapshot)
                    except Exception as _vm_err:
                        log.debug(f"RESEARCH_MANAGER | import/call error (ignored): {_vm_err}")

                    # FLO-239: Save verdict to file for get_oracle_verdict tool
                    # (removed from trigger_context to prevent confirmation bias — FLO-179 principle)
                    try:
                        _v = _verdict_result if (_verdict_result and _verdict_result.get("status") == "OK") else None
                        _verdict_save = {
                            "timestamp": utc_iso(),  # FLO-309
                            "winner": _v["winner"] if _v else None,
                            "recommendation": _v["recommendation"] if _v else None,
                            "conviction": _v["conviction"] if _v else None,
                            "reasoning": _v["reasoning"] if _v else "Research Manager unavailable",
                            "entry": _v.get("entry") if _v else None,
                            "sl": _v.get("sl") if _v else None,
                            "target": _v.get("target") if _v else None,
                            "trigger_buy": _v.get("trigger_buy") if _v else None,
                            "trigger_sell": _v.get("trigger_sell") if _v else None,
                            "rex_bull": _bull,
                            "rex_bear": _bear,
                        }
                        _vp = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "oracle_verdict.json")
                        _vt = _vp + ".tmp"
                        with open(_vt, "w", encoding="utf-8") as _vf:
                            json.dump(_verdict_save, _vf, ensure_ascii=False, indent=2)
                        os.replace(_vt, _vp)
                    except Exception:
                        pass
             except Exception as _deb_err:
                log.debug(f"REX_DEBATE | injection error (ignored): {_deb_err}")

             # Write debate + verdict to bot_state for dashboard
             try:
                if _debate_result and isinstance(_debate_result, dict):
                    self._last_debate_result = _debate_result
                if _verdict_result and isinstance(_verdict_result, dict):
                    self._last_verdict_result = _verdict_result
             except Exception:
                pass
            else:
                log.info("REX_DEBATE | SKIPPED — position open, RM only for entry decisions")

            # FLO-139: Inject market regime into trigger_context
            try:
                _regime = getattr(self, "_last_regime_context", None)
                if _regime and isinstance(_regime, dict) and _regime.get("regime"):
                    _r = _regime
                    _rn = _r["regime"]
                    _adx_val = _r.get("adx")
                    _dur = _r.get("duration_display") or "just started"
                    _prev = _r.get("previous_regime") or "unknown"
                    _stab = _r.get("stability") or "unknown"
                    _ch24 = _r.get("regime_changes_24h", 0)
                    _atr_c = _r.get("atr_current")
                    _atr_r = _r.get("atr_ratio", 1.0)
                    _trans = _r.get("transition") or f"Transitioned from {_prev}"
                    _ev = _r.get("evidence", [])
                    if _ev:
                        _evidence_str = ", ".join(str(e) for e in _ev[:5])
                    else:
                        _evidence_str = {
                            "RANGING": f"ADX {_adx_val or '?'}, low directional conviction, price between support and resistance",
                            "TRENDING_BULLISH": f"ADX {_adx_val or '?'}, bullish EMA alignment, price above EMA50",
                            "TRENDING_BEARISH": f"ADX {_adx_val or '?'}, bearish EMA alignment, price below EMA50",
                            "BREAKOUT_IMMINENT": f"Volatility compressing, ADX {_adx_val or '?'} rising from low base",
                            "VOLATILE": f"ATR elevated, large candles, ADX {_adx_val or '?'}",
                            "TRANSITIONAL": f"Regime shifting, ADX {_adx_val or '?'}, mixed signals",
                            "QUIET": f"Very low volume and ATR, ADX {_adx_val or '?'}",
                        }.get(_rn, f"ADX {_adx_val or '?'}")
                    _regime_ctx = {
                        "RANGING": "RANGING means price oscillates between support and resistance with no clear direction. False breakouts are common. Support and resistance levels are the key reference points.",
                        "TRENDING_BULLISH": "TRENDING BULLISH means price is in a sustained uptrend. Pullbacks to support are opportunities. Selling tops is risky.",
                        "TRENDING_BEARISH": "TRENDING BEARISH means price is in a sustained downtrend. Buying dips is risky. The trend is your friend until it changes.",
                        "BREAKOUT_IMMINENT": "BREAKOUT IMMINENT means volatility is compressing. A large move is likely soon. Wait for confirmation before acting.",
                        "VOLATILE": "VOLATILE means large erratic swings. Risk is elevated. Stops may get hit by noise.",
                        "TRANSITIONAL": "TRANSITIONAL means the regime is changing. The previous pattern may no longer hold. Wait for the new regime to establish.",
                        "QUIET": "QUIET means very low activity. Volume is thin. Moves may be unreliable.",
                    }.get(_rn, "")
                    _regime_block = (
                        f"\n<market_regime>\n"
                        f"Current: {_rn} ({_r.get('confidence', 'moderate')} confidence)\n"
                        f"Duration: {_dur} (since transition from {_prev})\n"
                        f"Stability: {_stab} ({_ch24} changes in 24h)\n"
                        f"ADX: {_adx_val or 'N/A'}\n"
                        f"ATR: {_atr_c or 'N/A'} pips ({_atr_r}x vs 5-day avg)\n"
                        f"Evidence: {_evidence_str}\n"
                    )
                    if _regime_ctx:
                        _regime_block += f"\n{_regime_ctx}\n"
                    _regime_block += f"</market_regime>\n"
                    trigger_context += _regime_block
            except Exception:
                pass

            # FLO-164 Evolution: Rich market structure + candle data
            try:
                import MetaTrader5 as _mt5_ms
                import numpy as _np_ms
                from datetime import datetime as _dt_ms

                def _calc_rsi(closes, period=14):
                    if len(closes) < period + 1:
                        return None
                    d = _np_ms.diff(closes)
                    g = float(_np_ms.mean(_np_ms.where(d > 0, d, 0)[-period:]))
                    l = float(_np_ms.mean(_np_ms.where(d < 0, -d, 0)[-period:]))
                    return round(100 - (100 / (1 + g / l)), 0) if l > 0 else 100

                def _calc_ema(closes, period=50):
                    ema = closes[0]
                    m = 2.0 / (period + 1)
                    for c in closes[1:]:
                        ema = c * m + ema * (1 - m)
                    return round(ema, 0)

                def _find_swings(highs, lows, n=5):
                    """Find swing high/low in last n bars."""
                    sh_idx = int(_np_ms.argmax(highs[-n:])) + len(highs) - n
                    sl_idx = int(_np_ms.argmin(lows[-n:])) + len(lows) - n
                    return sh_idx, sl_idx

                def _trend_label(highs, lows, n):
                    """Count higher highs/lows pattern."""
                    hh = 0; hl = 0; lh = 0; ll = 0
                    for i in range(1, min(n, len(highs))):
                        if highs[-i] > highs[-i-1]: hh += 1
                        else: lh += 1
                        if lows[-i] > lows[-i-1]: hl += 1
                        else: ll += 1
                    if hh >= n//2 and hl >= n//2: return f"UPTREND ({hh} higher highs, {hl} higher lows)"
                    if lh >= n//2 and ll >= n//2: return f"DOWNTREND ({lh} lower highs, {ll} lower lows)"
                    return "RANGING"

                def _tf_block(tf_enum, n_candles, n_display, label, current_price):
                    bars = _mt5_ms.copy_rates_from_pos("XAUUSD", tf_enum, 0, max(n_candles, 50) + 15)
                    if bars is None or len(bars) < n_candles:
                        return f"{label}: insufficient data", ""

                    closes = [float(b[4]) for b in bars]
                    highs = [float(b[2]) for b in bars]
                    lows = [float(b[3]) for b in bars]
                    recent_highs = highs[-n_candles:]
                    recent_lows = lows[-n_candles:]

                    trend = _trend_label(recent_highs, recent_lows, n_candles)
                    rsi = _calc_rsi(closes)
                    rsi_5ago = _calc_rsi(closes[:-5]) if len(closes) > 19 else rsi
                    rsi_dir = "rising" if rsi and rsi_5ago and rsi - rsi_5ago > 3 else ("falling" if rsi and rsi_5ago and rsi_5ago - rsi > 3 else "flat")
                    ema50 = _calc_ema(closes, 50)
                    ema200 = _calc_ema(closes, 200) if len(closes) >= 200 else None

                    sh_idx, sl_idx = _find_swings(highs, lows, n_candles)
                    sh_price = round(highs[sh_idx])
                    sl_price = round(lows[sl_idx])
                    sh_dist = round(sh_price - current_price)
                    sl_dist = round(current_price - sl_price)

                    # Rejection count for swing high (how many bars touched within 10 pts)
                    rejections = sum(1 for h in recent_highs if abs(h - sh_price) < 10)

                    pos = f"price {'ABOVE' if current_price > ema50 else 'BELOW'} EMA50 ({ema50})"
                    if ema200:
                        pos += f" and {'ABOVE' if current_price > ema200 else 'BELOW'} EMA200 ({ema200})"

                    summary = (
                        f"{label}: {trend}\n"
                        f"  Swing high: ${sh_price} ({'+' if sh_dist >= 0 else ''}{sh_dist} from price)"
                        + (f" — rejected {rejections}x" if rejections >= 2 else "") +
                        f" | Swing low: ${sl_price} (-{sl_dist} below)\n"
                        f"  RSI: {rsi} {rsi_dir} | {pos}"
                    )

                    # Compact candle array for context
                    display = bars[-n_display:]
                    candle_lines = []
                    for b in display:
                        t = _dt_ms.fromtimestamp(int(b[0])).strftime("%b%d %H:%M") if label == "H4" else _dt_ms.fromtimestamp(int(b[0])).strftime("%b%d")
                        candle_lines.append(f'{{t:"{t}",o:{round(b[1])},h:{round(b[2])},l:{round(b[3])},c:{round(b[4])},v:{int(b[5])}}}')
                    candle_block = f"<{label.lower()}_candles>\n[{','.join(candle_lines)}]\n</{label.lower()}_candles>"

                    return summary, candle_block

                _cp = float(getattr(self, "last_known_price", 0) or 0)
                if _cp > 0:
                    h4_summary, h4_candles = _tf_block(_mt5_ms.TIMEFRAME_H4, 20, 20, "H4", _cp)
                    d1_summary, d1_candles = _tf_block(_mt5_ms.TIMEFRAME_D1, 10, 10, "D1", _cp)

                    # -----------------------------------------------------------
                    # FLO-167: Volume profile + momentum quality (H1 bars)
                    # -----------------------------------------------------------
                    volume_block = ""
                    try:
                        h1_bars = _mt5_ms.copy_rates_from_pos("XAUUSD", _mt5_ms.TIMEFRAME_H1, 0, 50)
                        if h1_bars is not None and len(h1_bars) >= 10:
                            h1_vols = [int(b[5]) for b in h1_bars]
                            h1_highs = [float(b[2]) for b in h1_bars]
                            h1_lows = [float(b[3]) for b in h1_bars]
                            h1_closes = [float(b[4]) for b in h1_bars]

                            # Volume trend: last 5 vs previous 5
                            avg_last5 = sum(h1_vols[-5:]) / 5
                            avg_prev5 = sum(h1_vols[-10:-5]) / 5
                            if avg_prev5 > 0:
                                vol_ratio = avg_last5 / avg_prev5
                                if vol_ratio > 1.2:
                                    vol_trend = "increasing"
                                elif vol_ratio < 0.8:
                                    vol_trend = "decreasing"
                                else:
                                    vol_trend = "flat"
                            else:
                                vol_trend = "unknown"
                                vol_ratio = 0

                            # Volume at swing high
                            avg_vol = sum(h1_vols[-20:]) / min(len(h1_vols), 20)
                            sh_idx = int(_np_ms.argmax(h1_highs[-20:])) + max(0, len(h1_highs) - 20)
                            sh_vol = h1_vols[sh_idx]
                            if avg_vol > 0 and sh_vol > avg_vol * 1.2:
                                sh_vol_label = f"high_volume_rejection ({sh_vol:,} vs avg {int(avg_vol):,})"
                            else:
                                sh_vol_label = f"low_volume_test ({sh_vol:,} vs avg {int(avg_vol):,})"

                            # ATR (H1) for momentum quality
                            atr_vals = [h1_highs[i] - h1_lows[i] for i in range(len(h1_highs))]
                            atr_avg = sum(atr_vals[-14:]) / min(len(atr_vals), 14)
                            atr_5d_avg = sum(atr_vals[-120:]) / min(len(atr_vals), 120) if len(atr_vals) >= 20 else atr_avg

                            vol_rising = vol_trend == "increasing"
                            atr_above = atr_avg > atr_5d_avg * 1.1
                            if vol_rising and atr_above:
                                momentum_q = "strong (rising volume + above-avg ATR)"
                            elif vol_trend == "decreasing" and atr_avg < atr_5d_avg * 0.9:
                                momentum_q = "weak (declining volume + below-avg ATR)"
                            else:
                                momentum_q = "moderate"

                            volume_block = (
                                f"\nVOLUME: trend {vol_trend} (last 5 avg {int(avg_last5):,} vs prior 5 avg {int(avg_prev5):,})"
                                f", swing high {sh_vol_label}"
                                f"\nMOMENTUM: {momentum_q}"
                            )
                    except Exception:
                        pass

                    # -----------------------------------------------------------
                    # FLO-168: Enhanced confluence (fibs, round numbers, rejection weighting)
                    # -----------------------------------------------------------
                    confluence = ""
                    try:
                        # Collect levels with metadata: (price, label)
                        level_entries_res = []  # [(price, label), ...]
                        level_entries_sup = []

                        # S/R zones with rejection data
                        sr = getattr(self, "_last_agent_data", None)
                        if callable(sr):
                            sr = sr()
                        sr_zones = (sr or {}).get("sr_zones", [])
                        if isinstance(sr_zones, dict):
                            sr_zones = sr_zones.get("zones", [])
                        sr_zone_meta = {}  # price → {touches, last_touch_age_bars, tf}
                        for z in (sr_zones or [])[:30]:
                            if not isinstance(z, dict):
                                continue
                            mid = z.get("midpoint") or z.get("price", 0)
                            if not mid:
                                continue
                            mid = round(float(mid))
                            touches = int(z.get("touches") or z.get("rejections") or 0)
                            age = z.get("age_bars")
                            tf_label = z.get("timeframe") or "S/R"
                            sr_zone_meta[mid] = {"touches": touches, "age_bars": age, "tf": tf_label}
                            label = f"{tf_label} S/R"
                            if mid > _cp:
                                level_entries_res.append((mid, label))
                            else:
                                level_entries_sup.append((mid, label))

                        # Add EMAs as levels
                        h4_bars_raw = _mt5_ms.copy_rates_from_pos("XAUUSD", _mt5_ms.TIMEFRAME_H4, 0, 60)
                        h4_ema50 = _calc_ema([float(b[4]) for b in h4_bars_raw or []], 50)
                        if h4_ema50 < _cp:
                            level_entries_sup.append((h4_ema50, "H4 EMA50"))
                        else:
                            level_entries_res.append((h4_ema50, "H4 EMA50"))

                        # Fibonacci levels (multi-timeframe)
                        fib_dp = getattr(self, "_last_agent_data", None)
                        if callable(fib_dp):
                            fib_dp = fib_dp()
                        fib_data = (fib_dp or {}).get("fibonacci") or {}
                        if isinstance(fib_data, dict):
                            for tf_key, tf_fib in fib_data.items():
                                if not isinstance(tf_fib, dict):
                                    continue
                                levels = tf_fib.get("levels") or {}
                                for pct, price in levels.items():
                                    try:
                                        p = round(float(price))
                                        label = f"Fib {pct}% {tf_key}"
                                        if p > _cp:
                                            level_entries_res.append((p, label))
                                        else:
                                            level_entries_sup.append((p, label))
                                    except Exception:
                                        pass

                        def _cluster_enhanced(entries, n=3):
                            """Cluster levels within 50 pips, include labels and strength."""
                            if not entries:
                                return []
                            entries.sort(key=lambda x: x[0])
                            clusters = []
                            used = set()
                            for i, (price, label) in enumerate(entries):
                                if i in used:
                                    continue
                                nearby = [(j, p, lb) for j, (p, lb) in enumerate(entries) if abs(p - price) <= 50]
                                if len(nearby) < 2:
                                    continue
                                for j, _, _ in nearby:
                                    used.add(j)
                                low = min(p for _, p, _ in nearby)
                                high = max(p for _, p, _ in nearby)
                                labels = [lb for _, _, lb in nearby]

                                # Round number check ($xx00, $xx50 within 30 pips)
                                for rn in range(int(low / 50) * 50, int(high / 50 + 1) * 50 + 1, 50):
                                    if rn > 0 and any(abs(p - rn) <= 30 for _, p, _ in nearby):
                                        if rn not in [p for _, p, _ in nearby]:
                                            labels.append(f"round ${rn}")

                                # Rejection weighting from S/R zone metadata
                                strength = ""
                                best_touches = 0
                                recent_touch = False
                                for _, p, _ in nearby:
                                    meta = sr_zone_meta.get(round(p))
                                    if meta:
                                        t = meta["touches"]
                                        if t > best_touches:
                                            best_touches = t
                                        age = meta.get("age_bars")
                                        if age is not None and int(age) <= 48:
                                            recent_touch = True

                                if best_touches >= 20:
                                    strength = "EXTREME"
                                elif best_touches >= 10 and recent_touch:
                                    strength = "STRONG"
                                elif best_touches >= 5:
                                    strength = "MODERATE"
                                elif best_touches > 0:
                                    strength = "WEAK"

                                touch_info = ""
                                if best_touches > 0:
                                    touch_info = f", {best_touches} rejections"
                                    if recent_touch:
                                        touch_info += " (recent)"

                                desc = f"${low}-{high}"
                                if strength:
                                    desc += f" [{strength} — {' + '.join(labels[:4])}{touch_info}]"
                                else:
                                    desc += f" [{' + '.join(labels[:4])}]"
                                clusters.append((len(nearby), desc))
                            clusters.sort(key=lambda x: -x[0])
                            return [c[1] for c in clusters[:n]]

                        res_clusters = _cluster_enhanced(level_entries_res)
                        sup_clusters = _cluster_enhanced(level_entries_sup)
                        if res_clusters or sup_clusters:
                            parts = []
                            if res_clusters:
                                parts.append(f"RESISTANCE: {', '.join(res_clusters)}")
                            if sup_clusters:
                                parts.append(f"SUPPORT: {', '.join(sup_clusters)}")
                            confluence = "\nCONFLUENCE " + " | ".join(parts)
                    except Exception:
                        pass

                    # -----------------------------------------------------------
                    # FLO-165: Multi-candle pattern detection (double top/bottom, failed breakout)
                    # -----------------------------------------------------------
                    patterns_block = ""
                    try:
                        h4_bars_pat = _mt5_ms.copy_rates_from_pos("XAUUSD", _mt5_ms.TIMEFRAME_H4, 0, 30)
                        if h4_bars_pat is not None and len(h4_bars_pat) >= 10:
                            pat_highs = [float(b[2]) for b in h4_bars_pat]
                            pat_lows = [float(b[3]) for b in h4_bars_pat]
                            pat_closes = [float(b[4]) for b in h4_bars_pat]
                            pat_times = [_dt_ms.fromtimestamp(int(b[0])) for b in h4_bars_pat]
                            n_bars = len(pat_highs)

                            # Find all swing highs/lows (local max/min with 2 bars on each side)
                            swing_highs = []  # (index, price)
                            swing_lows = []
                            for i in range(2, n_bars - 2):
                                if pat_highs[i] >= max(pat_highs[i-2:i]) and pat_highs[i] >= max(pat_highs[i+1:i+3]):
                                    swing_highs.append((i, pat_highs[i]))
                                if pat_lows[i] <= min(pat_lows[i-2:i]) and pat_lows[i] <= min(pat_lows[i+1:i+3]):
                                    swing_lows.append((i, pat_lows[i]))

                            detected = []

                            # Double top: two swing highs within 50 pips, separated by 3+ bars
                            for a in range(len(swing_highs)):
                                for b_idx in range(a + 1, len(swing_highs)):
                                    ai, ap = swing_highs[a]
                                    bi, bp = swing_highs[b_idx]
                                    if bi - ai >= 3 and abs(ap - bp) <= 50:
                                        avg_top = round((ap + bp) / 2)
                                        dist = round(avg_top - _cp)
                                        if dist > 0 and dist < 200:
                                            detected.append(
                                                f"Double top forming at ${avg_top} "
                                                f"(swing highs ${round(ap)} + ${round(bp)}, "
                                                f"+{dist} from price)"
                                            )

                            # Double bottom: two swing lows within 50 pips, separated by 3+ bars
                            for a in range(len(swing_lows)):
                                for b_idx in range(a + 1, len(swing_lows)):
                                    ai, ap = swing_lows[a]
                                    bi, bp = swing_lows[b_idx]
                                    if bi - ai >= 3 and abs(ap - bp) <= 50:
                                        avg_bot = round((ap + bp) / 2)
                                        dist = round(_cp - avg_bot)
                                        if dist > 0 and dist < 200:
                                            detected.append(
                                                f"Double bottom forming at ${avg_bot} "
                                                f"(swing lows ${round(ap)} + ${round(bp)}, "
                                                f"-{dist} below price)"
                                            )

                            # Failed breakout: price closed above a swing high then back below within 3 bars
                            for si, sp in swing_highs:
                                # Look for close above the swing high after it formed
                                for j in range(si + 1, min(si + 6, n_bars)):
                                    if pat_closes[j] > sp:
                                        # Found a close above — now check if it failed back within 3 bars
                                        for k in range(j + 1, min(j + 4, n_bars)):
                                            if pat_closes[k] < sp:
                                                t_break = pat_times[j].strftime("%b%d %H:%M")
                                                t_fail = pat_times[k].strftime("%b%d %H:%M")
                                                detected.append(
                                                    f"Failed breakout at ${round(sp)} "
                                                    f"(broke above at {t_break}, failed back below by {t_fail})"
                                                )
                                                break
                                        break  # Only check first breakout attempt per swing high

                            # Head & Shoulders: middle swing high is highest, two flanking lows form neckline
                            try:
                                for a in range(len(swing_highs) - 2):
                                    ai, ap = swing_highs[a]
                                    bi, bp = swing_highs[a + 1]
                                    ci, cp = swing_highs[a + 2]
                                    if bp > ap and bp > cp and bi - ai >= 3 and ci - bi >= 3:
                                        # Find swing lows between the three highs for neckline
                                        neck_lows = [lp for li, lp in swing_lows if ai < li < ci]
                                        if len(neck_lows) >= 2:
                                            neckline = round(sum(neck_lows[:2]) / 2)
                                            if abs(neck_lows[0] - neck_lows[1]) <= 50:
                                                dist = round(_cp - neckline)
                                                if 0 < dist < 150:
                                                    detected.append(
                                                        f"H&S forming, neckline at ${neckline} "
                                                        f"(head ${round(bp)}, shoulders ${round(ap)} + ${round(cp)}, "
                                                        f"price {dist} above neckline)"
                                                    )
                            except Exception:
                                pass

                            # Rising wedge: higher highs + higher lows but range narrowing (bearish)
                            try:
                                if len(swing_highs) >= 3 and len(swing_lows) >= 3:
                                    sh3 = swing_highs[-3:]
                                    sl3 = swing_lows[-3:]
                                    hh_rising = sh3[0][1] < sh3[1][1] < sh3[2][1]
                                    hl_rising = sl3[0][1] < sl3[1][1] < sl3[2][1]
                                    range_first = sh3[0][1] - sl3[0][1]
                                    range_last = sh3[2][1] - sl3[2][1]
                                    if hh_rising and hl_rising and range_first > 0 and range_last < range_first * 0.8:
                                        detected.append(
                                            f"Rising wedge forming between ${round(sl3[2][1])}-${round(sh3[2][1])} "
                                            f"(bearish, range narrowing {round(range_first)}->{round(range_last)} pips)"
                                        )
                            except Exception:
                                pass

                            # Falling wedge: lower highs + lower lows but range narrowing (bullish)
                            try:
                                if len(swing_highs) >= 3 and len(swing_lows) >= 3:
                                    sh3 = swing_highs[-3:]
                                    sl3 = swing_lows[-3:]
                                    lh_falling = sh3[0][1] > sh3[1][1] > sh3[2][1]
                                    ll_falling = sl3[0][1] > sl3[1][1] > sl3[2][1]
                                    range_first = sh3[0][1] - sl3[0][1]
                                    range_last = sh3[2][1] - sl3[2][1]
                                    if lh_falling and ll_falling and range_first > 0 and range_last < range_first * 0.8:
                                        detected.append(
                                            f"Falling wedge forming between ${round(sl3[2][1])}-${round(sh3[2][1])} "
                                            f"(bullish, range narrowing {round(range_first)}->{round(range_last)} pips)"
                                        )
                            except Exception:
                                pass

                            # Channel: swing highs at similar levels AND swing lows at similar levels
                            try:
                                if len(swing_highs) >= 2 and len(swing_lows) >= 2:
                                    sh_prices = [p for _, p in swing_highs[-4:]]
                                    sl_prices = [p for _, p in swing_lows[-4:]]
                                    sh_range = max(sh_prices) - min(sh_prices)
                                    sl_range = max(sl_prices) - min(sl_prices)
                                    if sh_range <= 50 and sl_range <= 50:
                                        ch_top = round(sum(sh_prices) / len(sh_prices))
                                        ch_bot = round(sum(sl_prices) / len(sl_prices))
                                        if ch_top - ch_bot > 20:
                                            detected.append(
                                                f"Channel ${ch_bot}-${ch_top} "
                                                f"({len(sh_prices)} highs within {round(sh_range)} pips, "
                                                f"{len(sl_prices)} lows within {round(sl_range)} pips)"
                                            )
                            except Exception:
                                pass

                            if detected:
                                # Dedupe and limit
                                seen_pat = set()
                                unique_pats = []
                                for d in detected:
                                    key = d[:30]
                                    if key not in seen_pat:
                                        seen_pat.add(key)
                                        unique_pats.append(d)
                                patterns_block = "\nPATTERNS: " + ". ".join(unique_pats[:6])
                    except Exception:
                        pass

                    trigger_context += (
                        f"\n<market_structure>\n{d1_summary}\n\n{h4_summary}"
                        f"{volume_block}{confluence}{patterns_block}\n</market_structure>\n"
                        f"\n{h4_candles}\n{d1_candles}\n"
                    )
            except Exception:
                pass

            # FLO-243: Oracle verdict — no longer force-injected.
            # Oracle had 44% accuracy in live; forcing it may hurt Qwen's decisions.
            # Floki can call get_oracle_verdict tool when he wants advisory input.

            # FLO-269: Inject last trade report (hard data, no advice)
            try:
                from trade_reflexion import get_last_trade_report_summary
                _trade_report = get_last_trade_report_summary()
                if _trade_report:
                    trigger_context += f"\n{_trade_report}\n"
            except Exception:
                pass

            # Advisory-only close-window warning (buffers no longer block; Floki decides).
            try:
                _close_warn = safety_checks.get_market_close_warning()
                if _close_warn:
                    trigger_context += f"\n<market_warning>\n{_close_warn}\n</market_warning>\n"
            except Exception:
                pass

            # Self-assessment prompt — diagnostic only
            # FLO-302: appended as-is from agent_prompts.SELF_ASSESSMENT_PROMPT
            # to keep scanner + position modes byte-identical.
            from agent_prompts import SELF_ASSESSMENT_PROMPT as _SAP
            trigger_context += "\n" + _SAP

            # Position management mode — lighter context hint for faster cycles
            if _has_open_position:
                # FLO-314: removed Essential whitelist + DoNotCall blocklist.
                # Prior prompt trained Floki to treat "Essential tools: {7 names}"
                # as an implicit whitelist, causing him to skip non-forbidden
                # tools (luna/rex/calendar) in position cycles at 0.02/cycle vs
                # 0.59-0.75/cycle in scanner mode — a 30× drop. New text states
                # explicitly that all tools remain available and keeps only a
                # neutral latency note so Floki is informed, not instructed.
                trigger_context = (
                    "<position_mode>\n"
                    "You have an open position. Decide: HOLD_TRADE, ADJUST_TRADE, or CLOSE_TRADE.\n\n"
                    "All tools remain available — call what the situation needs.\n\n"
                    "Latency note: every tool call is time with your position exposed. If you need several tools, call them in one batch (parallel) rather than sequentially.\n"
                    "</position_mode>\n\n"
                ) + trigger_context
                log.info("FLOKI | position_mode=ON — all tools available, latency note injected")

            tools_obj = AgentTools(
                self,
                executor=executor,
                safety_checks_module=safety_checks,
                risk_manager_module=risk_manager,
            )

            # Inject last analysis-cycle candles snapshot (avoid MT5 calls in proactive context)
            try:
                cached = getattr(self, "_cached_candles", None)
                if isinstance(agent_data, dict) and isinstance(cached, dict) and cached:
                    agent_data["candles"] = dict(cached)
                    try:
                        self._last_agent_data = agent_data
                    except Exception:
                        pass
            except Exception:
                pass

            # Capture chart screenshots (pre-capture; Floki pulls via get_chart_screenshots tool)
            chart_images = None
            try:
                if getattr(config, 'CHART_SCREENSHOT_ENABLED', False):
                    chart_images = self._request_chart_screenshots(
                        timeout=getattr(config, 'CHART_SCREENSHOT_TIMEOUT', 10)
                    )
            except Exception as _ss_e:
                log.warning(f"SCREENSHOT | error: {_ss_e}")

            # Store pre-captured images on tools_obj for get_chart_screenshots tool
            if chart_images:
                tools_obj._chart_images = chart_images

            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

            pre_next_check_mtime = None
            try:
                if os.path.exists(next_path):
                    pre_next_check_mtime = os.path.getmtime(next_path)
            except Exception:
                pre_next_check_mtime = None

            agent_result = loop.run_until_complete(
                agent_decide(
                    trigger_context,
                    tools_obj,
                    trigger_type=trigger_type,
                    allow_memory_write=False,
                    chart_images=chart_images,
                )
            )
            loop.close()

            # FLO-273: Backfill Floki's decision into latest snapshot per open position
            try:
                _decision = getattr(agent_result, "decision", None)
                _conf = getattr(agent_result, "confidence", None)
                if _decision:
                    from db_writer import update_snapshot_floki_decision
                    _positions_for_snap = executor.get_open_positions() or []
                    for _p in _positions_for_snap:
                        try:
                            update_snapshot_floki_decision(
                                int(_p.ticket),
                                str(_decision),
                                int(_conf) if _conf is not None else None,
                            )
                        except Exception:
                            continue
            except Exception as e:
                log.debug(f"   Snapshot Floki backfill error (non-blocking): {e}")

            # A/B test: minimal vision-only call (Chamada B)
            try:
                if getattr(config, 'AB_TEST_ENABLED', False) and chart_images and chart_images.get("success"):
                    self._run_ab_test_minimal(chart_images, agent_result)
            except Exception:
                pass

            try:
                write_floki_heartbeat()
            except Exception:
                pass

            try:
                post_next_check_mtime = None
                try:
                    if os.path.exists(next_path):
                        post_next_check_mtime = os.path.getmtime(next_path)
                except Exception:
                    post_next_check_mtime = None

                positions_now = []
                try:
                    positions_now = get_positions() if self.executes_trades else []
                except Exception:
                    positions_now = []
                has_open_position = bool(positions_now)

                # FLO-241: Reset backoff if Floki called set_next_check
                if post_next_check_mtime != pre_next_check_mtime:
                    self._consecutive_no_timer = 0

                if post_next_check_mtime == pre_next_check_mtime:
                    # FLO-241: Progressive backoff when Floki doesn't set timer
                    _cnt = getattr(self, "_consecutive_no_timer", 0) + 1
                    self._consecutive_no_timer = _cnt
                    if has_open_position:
                        fallback_minutes = get_fallback_minutes(True)  # always short with position
                    else:
                        fallback_minutes = min(5 + ((_cnt - 1) * 5), 60)
                    now_utc = datetime.utcnow()
                    next_at = now_utc + timedelta(minutes=fallback_minutes)
                    payload = {
                        "next_check_at": next_at.isoformat(timespec="seconds") + "Z",
                        "requested_minutes": fallback_minutes,
                    }

                    try:
                        os.makedirs(os.path.dirname(next_path), exist_ok=True)
                        tmp_path = next_path + ".tmp"
                        with open(tmp_path, "w", encoding="utf-8") as f:
                            json.dump(payload, f, ensure_ascii=False, indent=2)
                        os.replace(tmp_path, next_path)
                        _mode_label = "position mode" if has_open_position else f"backoff #{_cnt}"
                        log.info(
                            f"FLOKI_SCHEDULE | Agent did not call set_next_check — defaulting to {fallback_minutes} minutes ({_mode_label})"
                        )
                    except Exception as e:
                        log.debug(f"FLOKI_SCHEDULE | default schedule write failed (ignored): {e}")
                elif has_open_position:
                    try:
                        with open(next_path, "r", encoding="utf-8") as f:
                            payload = json.load(f)
                        if isinstance(payload, dict):
                            requested_minutes = payload.get("requested_minutes")
                            capped_minutes = get_scheduled_minutes(requested_minutes, True)
                            if requested_minutes is None or int(capped_minutes) != int(requested_minutes):
                                now_utc = datetime.utcnow()
                                next_at = now_utc + timedelta(minutes=capped_minutes)
                                capped_payload = {
                                    "next_check_at": next_at.isoformat(timespec="seconds") + "Z",
                                    "requested_minutes": capped_minutes,
                                }
                                os.makedirs(os.path.dirname(next_path), exist_ok=True)
                                tmp_path = next_path + ".tmp"
                                with open(tmp_path, "w", encoding="utf-8") as f:
                                    json.dump(capped_payload, f, ensure_ascii=False, indent=2)
                                os.replace(tmp_path, next_path)
                                log.info(
                                    f"FLOKI_SCHEDULE | Open position cap applied — next check set to {capped_minutes} minutes"
                                )
                    except Exception as e:
                        log.debug(f"FLOKI_SCHEDULE | schedule cap write failed (ignored): {e}")
            except Exception:
                pass

            if agent_result.decision in ("REJECT", "DEFER_TO_BRAIN"):
                # Safe coercion: HOLD_TRADE if position open, WAIT if not
                _has_pos_coerce = False
                try:
                    _has_pos_coerce = bool(executor.get_open_positions() if self.executes_trades else [])
                except Exception:
                    pass
                _safe = "HOLD_TRADE" if _has_pos_coerce else "WAIT"
                log.info(f"PROACTIVE_H1 | Coerced '{agent_result.decision}' -> {_safe} (position={'yes' if _has_pos_coerce else 'no'})")
                agent_result.decision = _safe
        except Exception as e:
            log.warning(f"PROACTIVE_H1 | Agent call failed (non-blocking): {e}")
            return

        # FLO-108: Verify tool execution BEFORE persisting — if Floki decided OPEN/CLOSE
        # but didn't actually call the tool, rewrite the decision so all downstream
        # persists (DB, dashboard, events) reflect the effective WAIT/HOLD_TRADE.
        try:
            if agent_result.decision in ("OPEN_BUY", "OPEN_SELL"):
                _tool_trace = getattr(agent_result, "tool_trace", None) or []
                _exec_called = False
                _exec_succeeded = False
                for _t in _tool_trace:
                    if isinstance(_t, dict) and str(_t.get("name", "")).lower() == "execute_trade":
                        _exec_called = True
                        _r = _t.get("result")
                        if isinstance(_r, dict) and _r.get("success"):
                            _exec_succeeded = True
                        break

                if _exec_succeeded:
                    pass  # genuine open — decision stays
                elif _exec_called and not _exec_succeeded:
                    _fail_reason = "unknown"
                    try:
                        for _t in _tool_trace:
                            if isinstance(_t, dict) and str(_t.get("name", "")).lower() == "execute_trade":
                                _r = _t.get("result", {})
                                _fail_reason = _r.get("reason", "unknown") if isinstance(_r, dict) else "unknown"
                                break
                    except Exception:
                        pass
                    log.warning(
                        f"FLOKI | execute_trade called but FAILED: {_fail_reason} | "
                        f"conf={agent_result.confidence}"
                    )
                else:
                    log.info(
                        f"FLOKI | {agent_result.decision} without execute_trade call — treating as WAIT | "
                        f"conf={agent_result.confidence}"
                    )
                    agent_result.decision = "WAIT"

            elif agent_result.decision == "CLOSE_TRADE":
                _tool_trace_c = getattr(agent_result, "tool_trace", None) or []
                _close_called = False
                _close_succeeded = False
                for _tc in _tool_trace_c:
                    if isinstance(_tc, dict) and str(_tc.get("name", "")).lower() == "close_trade":
                        _close_called = True
                        _rc = _tc.get("result")
                        if isinstance(_rc, dict) and _rc.get("success"):
                            _close_succeeded = True
                        break

                if _close_succeeded:
                    pass  # genuine close — decision stays
                elif _close_called and not _close_succeeded:
                    _cfail = "unknown"
                    try:
                        for _tc in _tool_trace_c:
                            if isinstance(_tc, dict) and str(_tc.get("name", "")).lower() == "close_trade":
                                _rc = _tc.get("result", {})
                                _cfail = _rc.get("reason", "unknown") if isinstance(_rc, dict) else "unknown"
                                break
                    except Exception:
                        pass
                    log.warning(
                        f"FLOKI | close_trade called but FAILED: {_cfail} | conf={agent_result.confidence}"
                    )
                else:
                    log.info(
                        f"FLOKI | CLOSE_TRADE without close_trade call — treating as HOLD_TRADE | "
                        f"conf={agent_result.confidence}"
                    )
                    agent_result.decision = "HOLD_TRADE"
        except Exception:
            pass

        # FLO-103: Record trade open in SQLite whenever execute_trade succeeded —
        # regardless of decision type (OPEN_BUY, HOLD_TRADE, etc.). Covers the
        # case where Floki opens a trade mid-conversation via GEMINI_FOLLOWUP
        # but the final decision text says HOLD_TRADE.
        try:
            _tt = getattr(agent_result, "tool_trace", None) or []
            for _t in _tt:
                if isinstance(_t, dict) and str(_t.get("name", "")).lower() == "execute_trade":
                    _r = _t.get("result")
                    if isinstance(_r, dict) and _r.get("success") and _r.get("ticket"):
                        record_trade_open(
                            ticket=int(_r["ticket"]),
                            direction=str(_r.get("direction", "UNKNOWN")),
                            volume=float(_r.get("volume", 0.01)),
                            open_price=float(_r.get("fill_price", 0)),
                            sl=float(_r.get("sl", 0)),
                            tp=float(_r.get("tp", 0)),
                            comment="floki_agent",
                            decision_source="floki_agent",
                        )
                        log.info(
                            f"FLOKI | record_trade_open → ticket={_r['ticket']} "
                            f"{_r.get('direction', '?')} @ {_r.get('fill_price', '?')}"
                        )
                    break
        except Exception as e_rto:
            log.warning(f"FLOKI | record_trade_open failed: {e_rto}")

        # FLO-127: Persist active thesis for inter-cycle continuity
        try:
            import re as _re
            _reasoning = str(getattr(agent_result, "reasoning", "") or "")
            _decision = str(getattr(agent_result, "decision", "") or "")

            # Direction bias
            _upper_start = _reasoning[:150].upper()
            if _decision in ("OPEN_BUY",) or ("BUY" in _upper_start and "SELL" not in _upper_start):
                _bias = "BULLISH"
            elif _decision in ("OPEN_SELL",) or ("SELL" in _upper_start and "BUY" not in _upper_start):
                _bias = "BEARISH"
            else:
                _bias = "NEUTRAL"

            # Key levels (4xxx pattern for gold)
            _levels = sorted(set(int(m) for m in _re.findall(r'\b([2-9][0-9]{3})\b', _reasoning) if 2000 <= int(m) <= 9999))[:5]

            # Conditions (sentences with trigger words)
            _conditions = []
            for _sent in _reasoning.replace("!", ".").replace("?", ".").split("."):
                _s = _sent.strip().lower()
                if any(w in _s for w in ["wait for", "waiting for", "need to see", "want to see",
                                          "until we", "before i", "retest", "break of",
                                          "clear", "reclaim"]):
                    _conditions.append(_sent.strip()[:200])
                if len(_conditions) >= 3:
                    break

            # Invalidation
            _invalidation = None
            for _sent in _reasoning.replace("!", ".").replace("?", ".").split("."):
                _s = _sent.strip().lower()
                if any(w in _s for w in ["change my mind", "invalidate", "unless", "would flip"]):
                    _invalidation = _sent.strip()[:200]
                    break

            _cur_price = None
            try:
                _cur_price = float(getattr(self, "last_known_price", 0) or 0) or None
            except Exception:
                pass

            # Anti-repetition: compare with previous thesis
            _unchanged_since = None
            _thesis_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "active_thesis.json")
            try:
                if os.path.exists(_thesis_path):
                    with open(_thesis_path, "r", encoding="utf-8") as _tf:
                        _old = json.loads(_tf.read())
                    if isinstance(_old, dict):
                        _old_bias = _old.get("direction_bias")
                        _old_levels = set(_old.get("key_levels", []))
                        _new_levels = set(_levels)
                        _overlap = len(_old_levels & _new_levels) / max(len(_old_levels | _new_levels), 1)
                        if _old_bias == _bias and _overlap > 0.6:
                            _unchanged_since = _old.get("unchanged_since") or _old.get("timestamp")
            except Exception:
                pass

            _thesis = {
                "direction_bias": _bias,
                "key_levels": _levels,
                "conditions": _conditions,
                "invalidation": _invalidation,
                "decision": _decision,
                "confidence": getattr(agent_result, "confidence", None),
                "timestamp": datetime.utcnow().isoformat(timespec="seconds") + "Z",
                "price_at_decision": _cur_price,
                "unchanged_since": _unchanged_since,
            }
            _tmp = _thesis_path + ".tmp"
            with open(_tmp, "w", encoding="utf-8") as _tf:
                json.dump(_thesis, _tf, indent=2, ensure_ascii=False)
            os.replace(_tmp, _thesis_path)
        except Exception as e_thesis:
            log.warning(f"FLOKI | thesis persist failed: {e_thesis}")

        # FLO-185: Save cycle snapshot for delta injection in next cycle
        try:
            _snap = {"timestamp": datetime.utcnow().isoformat(timespec="seconds") + "Z"}
            _snap["price"] = _cur_price
            try:
                _bs_path_snap = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "bot_state.json")
                with open(_bs_path_snap, "r", encoding="utf-8") as _bsf:
                    _bs_snap = json.load(_bsf)
                _la_snap = _bs_snap.get("last_analysis", {})
                _ind_snap = _la_snap.get("indicators", {})
                _snap["rsi"] = _ind_snap.get("rsi_14")
                _snap["adx"] = _ind_snap.get("adx_14")
                _snap["macd_hist"] = _ind_snap.get("macd_hist")
                _snap["volume_ratio"] = _ind_snap.get("volume_ratio")
                _snap["atr"] = _ind_snap.get("atr_14")
                _mr_snap = _bs_snap.get("market_regime", {})
                _snap["regime"] = _mr_snap.get("regime")
            except Exception:
                pass
            try:
                _wc_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "agent_wake_conditions.json")
                if os.path.exists(_wc_path):
                    with open(_wc_path, "r", encoding="utf-8") as _wcf:
                        _wc = json.load(_wcf)
                    _snap["simba_conditions_count"] = len(_wc.get("conditions", []))
            except Exception:
                pass
            _snap_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "last_cycle_snapshot.json")
            _snap_tmp = _snap_path + ".tmp"
            with open(_snap_tmp, "w", encoding="utf-8") as _sf:
                json.dump(_snap, _sf, ensure_ascii=False)
            os.replace(_snap_tmp, _snap_path)
        except Exception:
            pass

        try:
            alert_proactive_decision(agent_result)
        except Exception as e:
            log.debug(f"{trigger_type} | Discord alert error (ignored): {e}")

        try:
            # Persist to SQLite
            record_agent_proactive_analysis(snapshot_time_iso, agent_result.to_dict())
        except Exception as e:
            log.warning(f"{trigger_type} | DB write error (ignored): {e}")

        try:
            # Store for dashboard
            if self.last_analysis and isinstance(self.last_analysis, dict):
                proactive_payload = {
                    "trigger": trigger_type,
                    "h1_close_time": snapshot_time_iso,
                    "timestamp": utc_iso(agent_result.timestamp) if agent_result.timestamp else utc_iso(),  # FLO-286
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

                try:
                    entry_conditions = getattr(agent_result, "entry_conditions", None)
                    if entry_conditions is not None:
                        proactive_payload["entry_conditions"] = entry_conditions
                except Exception:
                    pass

                if isinstance(trigger_data, dict) and trigger_data:
                    proactive_payload["trigger_data"] = trigger_data

                try:
                    if agent_result.decision in ("OPEN_BUY", "OPEN_SELL"):
                        tp = getattr(agent_result, "trade_plan", None)
                        if tp is not None:
                            proactive_payload["trade_plan"] = tp
                    elif agent_result.decision == "ADJUST_TRADE":
                        adj = getattr(agent_result, "adjustment", None)
                        if adj is not None:
                            proactive_payload["adjustment"] = adj
                    elif agent_result.decision == "CLOSE_TRADE":
                        cr = getattr(agent_result, "close_reason", None)
                        if cr is not None:
                            proactive_payload["close_reason"] = cr
                except Exception:
                    pass

                try:
                    _dn = getattr(agent_result, "data_needs", None)
                    if _dn:
                        proactive_payload["data_needs"] = _dn
                except Exception:
                    pass

                # FLO-302 step 4: dispatch to Discord (filtered + drift-tracked).
                # Fire-and-forget — never block the state update.
                try:
                    from data_needs_dispatcher import dispatch_data_needs as _ddn
                    _ticket_summary = None
                    try:
                        _pos = executor.get_open_positions() if self.executes_trades else []
                        if _pos:
                            _p = _pos[0]
                            _ticket_summary = f"#{_p.ticket} {_p.direction} {_p.open_price}"
                    except Exception:
                        pass
                    _ddn(
                        _dn if isinstance(_dn, dict) else None,
                        decision=agent_result.decision,
                        confidence=agent_result.confidence,
                        ticket_summary=_ticket_summary,
                        timestamp_utc=utc_iso(),
                    )
                except Exception as _e:
                    log.debug(f"data_needs dispatch error (ignored): {_e}")

                self.last_analysis["proactive_analysis"] = proactive_payload
        except Exception as e:
            log.debug(f"{trigger_type} | state update error (ignored): {e}")

        try:
            log.info(
                f"{trigger_type} | Agent decision: {agent_result.decision} | "
                f"conf={agent_result.confidence} | ts: {snapshot_time_iso}"
            )
        except Exception:
            pass

        try:
            from db_writer import record_agent_event

            d = str(getattr(agent_result, "decision", "") or "").strip()
            conf = getattr(agent_result, "confidence", None)
            reasoning = str(getattr(agent_result, "reasoning", "") or "").strip()

            conf_s = ""
            try:
                if conf is not None:
                    conf_s = f" ({int(round(float(conf)))}%)"
            except Exception:
                conf_s = ""

            # FLO-192: Full reasoning in chat feed (was truncated to 380 chars)
            reason_s = reasoning

            content = f"{d}{conf_s}. {reason_s}".strip()
            if content:
                # Extract tool names from trace for investigation panel
                _tt = getattr(agent_result, "tool_trace", None) or []
                _tools_used = []
                try:
                    _tools_used = [t.get("name") for t in _tt if isinstance(t, dict) and t.get("name")]
                except Exception:
                    pass

                record_agent_event(
                    "FLOKI_DECISION",
                    content[:4000],
                    payload={"trigger": trigger_type, "timestamp": snapshot_time_iso, "tools_used": _tools_used},
                    author="FLOKI",
                )

                # FLO-78: Discord card for OPEN decisions only
                if d in ("OPEN_BUY", "OPEN_SELL"):
                    try:
                        from discord_cards import build_floki_open_card, send_built_card
                        _tp = getattr(agent_result, "trade_plan", None) or {}
                        if isinstance(_tp, dict):
                            _entry = float(_tp.get("entry") or _tp.get("entry_price") or 0)
                            _sl = float(_tp.get("stop_loss") or 0)
                            _tpv = float(_tp.get("take_profit") or 0)
                        else:
                            _entry, _sl, _tpv = 0, 0, 0
                        _rex_v = getattr(agent_result, "rex_verdict", None)
                        _luna_b = None
                        _luna_r = None
                        try:
                            from luna_analyst import load_luna_brief
                            _lb = load_luna_brief()
                            if _lb:
                                _luna_b = _lb.get("environment")
                                _luna_r = _lb.get("risk_level")
                        except Exception:
                            pass
                        _sess = None
                        try:
                            _h = datetime.utcnow().hour
                            _sess = "Asian" if _h < 7 else ("London" if _h < 13 else ("NY" if _h < 22 else "Off"))
                        except Exception:
                            pass
                        send_built_card(build_floki_open_card(
                            direction="BUY" if d == "OPEN_BUY" else "SELL",
                            price=_entry,
                            confidence=float(agent_result.confidence or 0),
                            sl=_sl, tp=_tpv,
                            rex_verdict=str(_rex_v) if _rex_v else None,
                            luna_env=_luna_b, luna_risk=_luna_r, session=_sess,
                        ))
                    except Exception:
                        pass
        except Exception:
            pass

        # Log successful tool executions and handle ADJUST_TRADE
        # (OPEN/CLOSE verification already done above in FLO-108 block)
        try:
            if agent_result.decision in ("OPEN_BUY", "OPEN_SELL"):
                tp = getattr(agent_result, "trade_plan", None)
                exec_direction = "BUY" if agent_result.decision == "OPEN_BUY" else "SELL"
                stop_loss = tp.get("stop_loss") if isinstance(tp, dict) else None
                take_profit = tp.get("take_profit") if isinstance(tp, dict) else None
                log.info(
                    f"PROACTIVE_H1 | Agent OPEN executed via tool | {exec_direction} | SL={stop_loss} TP={take_profit} conf={agent_result.confidence}"
                )
            elif agent_result.decision == "CLOSE_TRADE":
                close_reason = getattr(agent_result, "close_reason", None) or "agent_close"
                log.info(
                    f"PROACTIVE_H1 | Agent CLOSE executed via tool | reason={close_reason} | conf={agent_result.confidence}"
                )
            elif agent_result.decision == "ADJUST_TRADE":
                adj = getattr(agent_result, "adjustment", None)
                if not isinstance(adj, dict):
                    log.warning("PROACTIVE_H1 | ADJUST_TRADE without adjustment payload — skipping")
                else:
                    new_sl = adj.get("new_sl")
                    new_tp = adj.get("new_tp")
                    adj_reason = adj.get("reason") or "agent_adjust"

                    if new_sl is None and new_tp is None:
                        log.warning("PROACTIVE_H1 | ADJUST_TRADE without new_sl/new_tp — skipping")
                    else:
                        log.info(
                            f"PROACTIVE_H1 | Agent ADJUST intent logged (tool executes) | new_sl={new_sl} new_tp={new_tp} reason={adj_reason} | conf={agent_result.confidence}"
                        )
        except Exception as e:
            log.warning(f"PROACTIVE_H1 | Agent execution error (ignored): {e}")
    
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
        
        log.info("   📊 Mode: CENTRAL SCANNER")
        
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
        # FLO-187: Skip ML prediction entirely when disabled
        if config.ML_ENABLED:
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
        else:
            log.info("ML | DISABLED via config — skipping prediction")
            ml_data = {
                "score": 50.0, "prediction": "neutral", "probability": 0.0,
                "max_confidence": 0.0, "pattern": "disabled",
                "similar_patterns_count": None, "historical_success_rate": None,
                "error": "ML_ENABLED=False",
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

                # FLO-262: Per-TF zone detection for separate chart JSON files
                try:
                    from support_resistance import detect_zones_per_tf
                    per_tf = detect_zones_per_tf(
                        df_h1_for_sr, df_h4, df_d1=df_d1,
                        merge_pips=config.SR_ZONE_MERGE_PIPS,
                        merge_pips_d1=config.SR_ZONE_MERGE_PIPS_D1,
                        confluence_pips=getattr(config, 'SR_CONFLUENCE_TOLERANCE_PIPS', 5),
                        max_age_bars=config.SR_ZONE_MAX_AGE_BARS,
                        min_touches=config.SR_MIN_TOUCHES,
                        lookback_h1=config.SR_LOOKBACK_H1,
                        lookback_h4=config.SR_LOOKBACK_H4,
                        lookback_d1=config.SR_LOOKBACK_D1,
                    )
                    self._last_sr_zones_per_tf = per_tf
                    self._write_sr_zones_per_tf_json(current_price, per_tf)
                except Exception as e:
                    log.debug(f"   S/R per-TF: error (non-blocking): {e}")

                # FLO-273: Trade snapshots — capture indicator state for open positions
                try:
                    positions_now = executor.get_open_positions() or []
                    if positions_now:
                        from db_writer import record_trade_snapshot
                        PIP = 0.1
                        last_row = df.iloc[-1] if df is not None and len(df) > 0 else None

                        # Nearest S/R helper (inline — scoped to this hook)
                        def _nearest_sr(zones, px):
                            if not zones:
                                return None
                            best = None
                            best_dist = 999999
                            for z in zones:
                                d = abs(z.midpoint - px)
                                if d < best_dist:
                                    best_dist = d
                                    best = z
                            if not best:
                                return None
                            zt = "SUP" if best.midpoint <= px else "RES"
                            return f"{best.midpoint:.2f} {zt} {best.touches}T {best_dist/PIP:.0f}pips"

                        def _bb_bucket(pos_label):
                            # tech_data uses "banda_superior" / "banda_inferior" / "meio"
                            if pos_label == "banda_superior":
                                return "above_upper"
                            if pos_label == "banda_inferior":
                                return "below_lower"
                            return "middle"

                        # Pull values once
                        _rsi = tech_data.get("rsi", {}).get("value") if isinstance(tech_data, dict) else None
                        _stoch_k = tech_data.get("stochastic", {}).get("value") if isinstance(tech_data, dict) else None
                        _stoch_d = float(last_row["stoch_d"]) if (last_row is not None and "stoch_d" in last_row.index) else None
                        # FLO-276: ADX lives in momentum_data["adx"]["adx_value"], not in df
                        _adx = None
                        try:
                            _adx_raw = momentum_data.get("adx", {}).get("adx_value") if isinstance(momentum_data, dict) else None
                            if _adx_raw is not None:
                                _adx = round(float(_adx_raw), 2)
                        except Exception:
                            pass
                        _vol_ratio = momentum_data.get("volume", {}).get("volume_ratio") if isinstance(momentum_data, dict) else None
                        _macd_hist = tech_data.get("macd", {}).get("histogram") if isinstance(tech_data, dict) else None
                        _bb_pos = _bb_bucket(tech_data.get("bollinger", {}).get("position")) if isinstance(tech_data, dict) else "middle"
                        _nearest = _nearest_sr(sr_zones, current_price)
                        _regime = None
                        try:
                            _regime = (self._last_regime_context or {}).get("regime")
                        except Exception:
                            pass

                        for _pos in positions_now:
                            try:
                                _entry = float(_pos.open_price)
                                _dir = str(_pos.direction or "").upper()
                                _ppips = (current_price - _entry) / PIP if _dir == "BUY" else (_entry - current_price) / PIP
                                record_trade_snapshot({
                                    "ticket": int(_pos.ticket),
                                    "timestamp": utc_iso(),  # FLO-286: UTC ISO with Z
                                    "price": round(current_price, 2),
                                    "profit_pips": round(_ppips, 1),
                                    "rsi": _rsi,
                                    "stochastic_k": _stoch_k,
                                    "stochastic_d": round(_stoch_d, 2) if _stoch_d is not None else None,
                                    "adx": round(_adx, 2) if _adx is not None else None,
                                    "volume_ratio": _vol_ratio,
                                    "macd_histogram": _macd_hist,
                                    "bb_position": _bb_pos,
                                    "nearest_sr": _nearest,
                                    "regime": _regime,
                                    "floki_decision": None,
                                    "floki_confidence": None,
                                })
                            except Exception:
                                continue
                        log.debug(f"   Snapshots: wrote {len(positions_now)} for open positions")
                except Exception as e:
                    log.debug(f"   Trade snapshots: error (non-blocking): {e}")

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
        # Detailed log
        log.info(f"   📊 Scenario: {brain_result.scenario_description}")

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
        
        # Determine direction (confidence gate removed in Phase 0)
        direction = None
        hold_forced = False
        original_decision = None
        hold_reason = None
        if is_actionable_signal(brain_result.decision):
            direction = get_trade_direction(brain_result.decision)
        
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

            self.last_analysis = {
                "timestamp": utc_iso(),  # FLO-286: was datetime.now() — LOCAL leaked into analyses table
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

        # FLO-139: Regime detection — runs every Brain cycle, result used by proactive agent
        try:
            from regime_detector import detect_market_regime
            atr_value = momentum_data.get("atr", {}).get("atr_value", 0)
            atr_history = getattr(self, '_atr_history', [])
            if atr_value:
                atr_history.append(atr_value)
                atr_history = atr_history[-120:]
                self._atr_history = atr_history

            luna_brief_data = None
            try:
                from luna_analyst import load_luna_brief
                luna_brief_data = load_luna_brief()
            except Exception:
                pass

            # FLO-151: Pass M5 data and H1 candles for fast regime detection
            _h1_candles_for_regime = None
            try:
                if df is not None and hasattr(df, 'values') and len(df) >= 3:
                    _h1_candles_for_regime = df[['open', 'high', 'low', 'close']].tail(10).to_dict('records')
            except Exception:
                pass

            regime_result = detect_market_regime(
                tech_data=tech_data,
                momentum_data=momentum_data,
                vol_status=vol_status,
                brain_result=brain_result,
                current_price=current_price,
                atr_history=atr_history,
                luna_brief=luna_brief_data,
                m5_data=m5_status,
                h1_candles=_h1_candles_for_regime,
            )
            self._last_regime_context = regime_result
            _regime_src = "fast" if "Fast detection" in str(regime_result.get("evidence", [])) else "ADX"
            log.info(
                f"REGIME | {regime_result['regime']} | {regime_result['confidence']} | "
                f"{regime_result['duration_display']} | {regime_result['stability']} | "
                f"ADX={regime_result.get('adx')} | ATR_ratio={regime_result.get('atr_ratio')} | src={_regime_src}"
            )
        except Exception as e:
            log.warning(f"REGIME | detection error: {e}")

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
                "timestamp": utc_iso(),  # FLO-286: was datetime.now() — LOCAL leaked into analyses table
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
    
    def _call_agent(
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
        Call AI Agent.
        Agent is the decision maker and executor; results are logged for monitoring.
        
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
        log.info(f"   🤖 Calling AI Agent (trigger={trigger_type})...")
        
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
            for i in range(max(0, len(df) - 50), len(df)):
                row = df.iloc[i]
                tv = 0
                try:
                    tv = int(row.get("tick_volume", row.get("volume", 0)) or 0)
                except Exception:
                    tv = 0
                h1_candles.append({
                    "time": str(row.get("datetime", "")),
                    "open": float(row["open"]),
                    "high": float(row["high"]),
                    "low": float(row["low"]),
                    "close": float(row["close"]),
                    "tick_volume": tv,
                })
            
            # M5 candles
            m5_rates = mt5.copy_rates_from_pos(config.SYMBOL, mt5.TIMEFRAME_M5, 0, 10)
            if m5_rates is not None:
                _tz_off = _mt5_server_offset()
                for r in m5_rates:
                    m5_candles.append({
                        "time": datetime.utcfromtimestamp(int(r["time"]) - _tz_off).isoformat(),
                        "open": float(r["open"]),
                        "high": float(r["high"]),
                        "low": float(r["low"]),
                        "close": float(r["close"]),
                        "tick_volume": int(r["tick_volume"]),
                    })

            # D1 candles (weekly context)
            d1_rates = mt5.copy_rates_from_pos(config.SYMBOL, mt5.TIMEFRAME_D1, 0, 10)
            if d1_rates is not None:
                _tz_off = _mt5_server_offset()
                for r in d1_rates:
                    d1_candles.append({
                        "time": datetime.utcfromtimestamp(int(r["time"]) - _tz_off).isoformat(),
                        "open": float(r["open"]),
                        "high": float(r["high"]),
                        "low": float(r["low"]),
                        "close": float(r["close"]),
                        "tick_volume": int(r["tick_volume"]),
                    })

            # H4 candles (2-3 day structure)
            h4_rates = mt5.copy_rates_from_pos(config.SYMBOL, mt5.TIMEFRAME_H4, 0, 20)
            if h4_rates is not None:
                _tz_off = _mt5_server_offset()
                for r in h4_rates:
                    h4_candles.append({
                        "time": datetime.utcfromtimestamp(int(r["time"]) - _tz_off).isoformat(),
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
        # FLO-139: Full regime detection with temporal context
        try:
            atr_value = momentum_data.get("atr", {}).get("atr_value", 0)
            atr_history = getattr(self, '_atr_history', [])
            if atr_value:
                atr_history.append(atr_value)
                atr_history = atr_history[-120:]
                self._atr_history = atr_history

            luna_brief_data = None
            try:
                from luna_analyst import load_luna_brief
                luna_brief_data = load_luna_brief()
            except Exception:
                pass

            from regime_detector import detect_market_regime
            regime_result = detect_market_regime(
                tech_data=tech_data,
                momentum_data=momentum_data,
                vol_status=vol_status,
                brain_result=brain_result,
                current_price=current_price,
                atr_history=atr_history,
                luna_brief=luna_brief_data,
            )
            self._last_regime_context = regime_result
            log.info(
                f"REGIME | {regime_result['regime']} | {regime_result['confidence']} | "
                f"{regime_result['duration_display']} | {regime_result['stability']} | "
                f"ADX={regime_result.get('adx')} | ATR_ratio={regime_result.get('atr_ratio')}"
            )
        except Exception as e:
            log.debug(f"Error in regime detection: {e}")
        
        # Call Agent (async) - tool-driven (no XML / no full data package)
        from ai_agent import agent_decide
        from agent_tools import AgentTools
        import safety_checks
        import risk_manager
        try:
            trigger_context = "Brain signaled an actionable condition. Investigate using tools and respond with final decision JSON."
            try:
                # Provide minimal context (no giant payload). Keep it short.
                session_name = session_context.get('session_name') if isinstance(session_context, dict) else ''
                trigger_context = (
                    f"Reactive analysis triggered. Session={session_name}. "
                    "Investigate using tools and respond with final decision JSON."
                )
            except Exception:
                pass

            # FLO-179: previous_thesis injection removed (reactive path, same as proactive).

            # FLO-185: Delta injection for reactive path (same logic as proactive)
            try:
                _snap_path_r = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "last_cycle_snapshot.json")
                if os.path.exists(_snap_path_r):
                    with open(_snap_path_r, "r", encoding="utf-8") as _sf:
                        _prev_snap_r = json.load(_sf)
                    if isinstance(_prev_snap_r, dict) and _prev_snap_r.get("price") is not None:
                        _cur_snap_r = {}
                        try:
                            _bs_path_r = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "bot_state.json")
                            with open(_bs_path_r, "r", encoding="utf-8") as _bsf:
                                _bs_r = json.load(_bsf)
                            _la_r = _bs_r.get("last_analysis", {})
                            _ind_r = _la_r.get("indicators", {})
                            _cur_snap_r["price"] = float(getattr(self, "last_known_price", 0) or 0) or None
                            _cur_snap_r["rsi"] = _ind_r.get("rsi_14")
                            _cur_snap_r["adx"] = _ind_r.get("adx_14")
                            _cur_snap_r["macd_hist"] = _ind_r.get("macd_hist")
                            _cur_snap_r["regime"] = _bs_r.get("market_regime", {}).get("regime")
                        except Exception:
                            pass
                        if _cur_snap_r.get("price") is not None:
                            _interval_r = ""
                            try:
                                _prev_ts_r = datetime.fromisoformat(_prev_snap_r["timestamp"].replace("Z", "+00:00"))
                                _elapsed_r = int((datetime.now(timezone.utc) - _prev_ts_r).total_seconds() / 60)
                                _interval_r = f' interval="{_elapsed_r}min"'
                            except Exception:
                                pass
                            _lines_r = []
                            for _key, _label, _fmt in [("price", "PRICE", ".0f"), ("rsi", "RSI", ".1f"), ("adx", "ADX", ".1f"), ("macd_hist", "MACD_HIST", ".2f")]:
                                _o = _prev_snap_r.get(_key); _n = _cur_snap_r.get(_key)
                                if _o is not None and _n is not None:
                                    try:
                                        _ov = float(_o); _nv = float(_n); _dv = _nv - _ov
                                        _lines_r.append(f"{_label}: {_ov:{_fmt}} -> {_nv:{_fmt}} ({'+' if _dv >= 0 else ''}{_dv:{_fmt}})")
                                    except Exception:
                                        pass
                            _or = _prev_snap_r.get("regime"); _nr = _cur_snap_r.get("regime")
                            if _or and _nr:
                                _lines_r.append(f"REGIME: {_nr} ({'CHANGED from ' + _or if _or != _nr else 'unchanged'})")
                            if _lines_r:
                                trigger_context += f"\n<since_last_cycle{_interval_r}>\n" + "\n".join(_lines_r) + "\n</since_last_cycle>\n"
            except Exception:
                pass

            # FLO-139: Inject market regime into reactive trigger_context (same as proactive)
            try:
                _regime = getattr(self, "_last_regime_context", None)
                if _regime and isinstance(_regime, dict) and _regime.get("regime"):
                    _r = _regime
                    _evidence_str = ", ".join(_r.get("evidence", [])[:5])
                    trigger_context += (
                        f"\n<market_regime>\n"
                        f"Current: {_r['regime']} ({_r.get('confidence', '?')} confidence)\n"
                        f"Duration: {_r.get('duration_display', '?')} (since transition from {_r.get('previous_regime', '?')})\n"
                        f"Stability: {_r.get('stability', '?')} ({_r.get('regime_changes_24h', 0)} changes in 24h)\n"
                        f"Evidence: {_evidence_str}\n"
                        f"ATR: {_r.get('atr_current', '?')} pips ({_r.get('atr_ratio', '?')}x vs 5-day avg)\n"
                        f"Transition: {_r.get('transition', '?')}\n"
                        f"</market_regime>\n"
                    )
            except Exception:
                pass

            # FLO-269: Inject last trade report (reactive path)
            try:
                from trade_reflexion import get_last_trade_report_summary
                _trade_report_r = get_last_trade_report_summary()
                if _trade_report_r:
                    trigger_context += f"\n{_trade_report_r}\n"
            except Exception:
                pass

            # Advisory-only close-window warning (buffers no longer block; Floki decides).
            try:
                _close_warn = safety_checks.get_market_close_warning()
                if _close_warn:
                    trigger_context += f"\n<market_warning>\n{_close_warn}\n</market_warning>\n"
            except Exception:
                pass

            # Self-assessment prompt — diagnostic only
            # FLO-302: appended as-is from agent_prompts.SELF_ASSESSMENT_PROMPT
            # to keep scanner + position modes byte-identical.
            from agent_prompts import SELF_ASSESSMENT_PROMPT as _SAP
            trigger_context += "\n" + _SAP

            tools_obj = AgentTools(
                self,
                executor=executor,
                safety_checks_module=safety_checks,
                risk_manager_module=risk_manager,
            )

            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            agent_result = loop.run_until_complete(
                agent_decide(
                    trigger_context,
                    tools_obj,
                )
            )
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
        
        # Agent is the executor
        executed = "AGENT"
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
            _ad = {
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
            _dn = getattr(agent_result, "data_needs", None)
            if _dn:
                _ad["data_needs"] = _dn
            # FLO-302 step 4: dispatch (fast-decision path).
            try:
                from data_needs_dispatcher import dispatch_data_needs as _ddn
                _ticket_summary = None
                try:
                    _pos = executor.get_open_positions() if self.executes_trades else []
                    if _pos:
                        _p = _pos[0]
                        _ticket_summary = f"#{_p.ticket} {_p.direction} {_p.open_price}"
                except Exception:
                    pass
                _ddn(
                    _dn if isinstance(_dn, dict) else None,
                    decision=agent_result.decision,
                    confidence=agent_result.confidence,
                    ticket_summary=_ticket_summary,
                    timestamp_utc=utc_iso(),
                )
            except Exception as _e:
                log.debug(f"data_needs dispatch error (ignored): {_e}")
            self.last_analysis["agent_decision"] = _ad
    
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

        _hb_sent = alert_heartbeat_full(
            bot_name=config.DISCORD_BOT_NAME,
            uptime=uptime,
            open_positions=open_positions,
            last_analysis_time=last_analysis_time,
        )
        # FLO-285: honest log — alert_heartbeat_full returns False when channel disabled
        if _hb_sent:
            log.info("   Heartbeat sent to Discord")
        else:
            log.debug("   Heartbeat suppressed (DISCORD_WEBHOOK_STATUS not configured)")
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

    # ── A/B Test: Full Floki vs Minimal Vision-Only ──

    _AB_TEST_MINIMAL_PROMPT = (
        "You are an expert XAU/USD intraday trader. You receive chart screenshots and basic market data.\n\n"
        "Look at the H1 and M15 charts carefully. Describe what you see: candle patterns, "
        "S/R line interactions, momentum, wicks, and structure.\n\n"
        "Current price: {price}\n"
        "Nearest support: {support}\n"
        "Nearest resistance: {resistance}\n"
        "RSI(H1): {rsi} | ADX: {adx} | MACD: {macd}\n\n"
        "Decide: BUY, SELL, or WAIT.\n"
        'Respond with JSON: {{"decision": "BUY/SELL/WAIT", "confidence": 0-100, "reasoning": "2-3 sentences"}}'
    )

    def _run_ab_test_minimal(self, chart_images: dict, agent_result) -> None:
        """Run minimal vision-only call (Chamada B) and save both results."""
        t0 = time.time()
        try:
            from openai import OpenAI
            api_key = os.environ.get("OPENAI_API_KEY", "")
            if not api_key:
                return

            # Read current price + indicators from bot_state
            _bs_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "bot_state.json")
            with open(_bs_path, "r", encoding="utf-8") as f:
                _bs = json.load(f)

            _mtf = _bs.get("multi_tf_indicators", {})
            _h1 = _mtf.get("H1", {})
            _price = _bs.get("last_known_price") or _bs.get("last_analysis", {}).get("current_price") or "?"
            _rsi = _h1.get("rsi", "?")
            _adx_d = _h1.get("adx", {})
            _adx = _adx_d.get("value", "?") if isinstance(_adx_d, dict) else _adx_d or "?"
            _macd_v = _h1.get("macd", {})
            if isinstance(_macd_v, dict):
                _macd = f"{_macd_v.get('value', '?')}"
            else:
                _macd = str(_macd_v) if _macd_v is not None else "?"

            # Read S/R zones
            _sr_path = getattr(config, "SR_ZONES_JSON_PATH", "")
            _support = []
            _resistance = []
            if _sr_path and os.path.exists(_sr_path):
                with open(_sr_path, "r", encoding="utf-8") as f:
                    _sr = json.load(f)
                for z in (_sr.get("zones") or []):
                    _label = f"{z.get('price', '?')} ({z.get('timeframe','?')} {z.get('zone_type','?')} {z.get('touches','?')}T)"
                    if z.get("position") == "below":
                        _support.append(_label)
                    else:
                        _resistance.append(_label)

            _prompt_text = self._AB_TEST_MINIMAL_PROMPT.format(
                price=_price,
                support=", ".join(_support[:3]) or "none nearby",
                resistance=", ".join(_resistance[:3]) or "none nearby",
                rsi=f"{_rsi:.1f}" if isinstance(_rsi, (int, float)) else str(_rsi),
                adx=f"{_adx:.1f}" if isinstance(_adx, (int, float)) else str(_adx),
                macd=_macd,
            )

            # Build content blocks with images
            content_blocks = [{"type": "text", "text": _prompt_text}]
            if chart_images.get("h1_b64"):
                content_blocks.append({"type": "image_url", "image_url": {"url": f"data:image/png;base64,{chart_images['h1_b64']}", "detail": "high"}})
                content_blocks.append({"type": "text", "text": "Above: XAUUSD H1 chart."})
            if chart_images.get("m15_b64"):
                content_blocks.append({"type": "image_url", "image_url": {"url": f"data:image/png;base64,{chart_images['m15_b64']}", "detail": "high"}})
                content_blocks.append({"type": "text", "text": "Above: XAUUSD M15 chart."})

            # B call always uses GPT-5.4 (comparing Qwen Full vs GPT Minimal)
            client = OpenAI(api_key=api_key)
            model = getattr(config, "FLOKI_FALLBACK_MODEL", "gpt-5.4")

            resp = client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": content_blocks}],
                max_completion_tokens=512,
                temperature=1.0,
                timeout=30,
            )

            _raw = (resp.choices[0].message.content or "").strip() if resp.choices else ""
            _b_tokens_in = resp.usage.prompt_tokens if resp.usage else 0
            _b_tokens_out = resp.usage.completion_tokens if resp.usage else 0

            # Parse JSON from response
            import re as _re_ab
            _b_result = None
            _json_match = _re_ab.search(r'\{[^{}]*"decision"[^{}]*\}', _raw)
            if _json_match:
                try:
                    _b_result = json.loads(_json_match.group())
                except Exception:
                    pass

            if not _b_result:
                log.warning(f"AB_TEST | B parse failed: {_raw[:200]}")
                return

            # Extract A result
            _a_decision = "?"
            _a_confidence = 0
            _a_reasoning = ""
            try:
                if hasattr(agent_result, "decision"):
                    _a_decision = agent_result.decision or "?"
                    _a_confidence = agent_result.confidence or 0
                    _a_reasoning = agent_result.reasoning or ""
            except Exception:
                pass

            # Build entry
            _entry = {
                "id": int(time.time()),
                "timestamp": utc_iso(),  # FLO-309
                "price_at_decision": float(_price) if isinstance(_price, (int, float)) else None,
                "test_a": {
                    "model": getattr(agent_result, 'model', None) or getattr(config, 'FLOKI_MODEL', '?'),
                    "decision": _a_decision,
                    "confidence": _a_confidence,
                    "reasoning": str(_a_reasoning)[:300],
                    "tokens": f"{getattr(agent_result, 'input_tokens', '?')}+{getattr(agent_result, 'output_tokens', '?')}",
                },
                "test_b": {
                    "model": model,
                    "decision": _b_result.get("decision", "?"),
                    "confidence": _b_result.get("confidence", 0),
                    "reasoning": str(_b_result.get("reasoning", ""))[:300],
                    "tokens": f"{_b_tokens_in}+{_b_tokens_out}",
                },
                "price_after_30m": None,
                "winner_a": None,
                "winner_b": None,
            }

            # Append to results file
            _ab_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "ab_test_results.json")
            _results = []
            if os.path.exists(_ab_path):
                try:
                    with open(_ab_path, "r", encoding="utf-8") as f:
                        _results = json.load(f)
                except Exception:
                    _results = []
            _results.append(_entry)

            _tmp = _ab_path + ".tmp"
            with open(_tmp, "w", encoding="utf-8") as f:
                json.dump(_results, f, ensure_ascii=False, indent=2)
            os.replace(_tmp, _ab_path)

            latency = time.time() - t0
            log.info(f"AB_TEST | B={_b_result.get('decision','?')}/{_b_result.get('confidence',0)} "
                     f"vs A={_a_decision}/{_a_confidence} | {_b_tokens_in}+{_b_tokens_out} tokens | {latency:.1f}s")

        except Exception as e:
            log.warning(f"AB_TEST | error: {e}")

    def _resolve_ab_test_entries(self) -> None:
        """Check old A/B test entries and fill in price_after_30m + winner."""
        try:
            _ab_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "ab_test_results.json")
            if not os.path.exists(_ab_path):
                return

            with open(_ab_path, "r", encoding="utf-8") as f:
                _results = json.load(f)

            if not isinstance(_results, list) or not _results:
                return

            # Get current price
            _cp = None
            try:
                import MetaTrader5 as mt5
                tick = mt5.symbol_info_tick("XAUUSD")
                if tick:
                    _cp = tick.bid
            except Exception:
                pass
            if not _cp:
                return

            _changed = False
            _now = time.time()
            _PIP_THRESHOLD = 5.0  # pips (0.1 per pip for XAUUSD = 0.5 price units)
            _PRICE_THRESHOLD = _PIP_THRESHOLD * 0.1

            for entry in _results:
                if entry.get("price_after_30m") is not None:
                    continue  # already resolved

                _ts_str = entry.get("timestamp", "")
                try:
                    from datetime import timezone
                    _ts = datetime.fromisoformat(_ts_str.replace("Z", "+00:00"))
                    _age_min = (_now - _ts.timestamp()) / 60
                except Exception:
                    continue

                if _age_min < 30:
                    continue  # too recent

                _price_at = entry.get("price_at_decision")
                if not _price_at:
                    continue

                entry["price_after_30m"] = round(_cp, 2)
                _move = _cp - _price_at

                # Evaluate each variant
                for key in ("test_a", "test_b"):
                    _d = entry.get(key, {}).get("decision", "").upper()
                    _winner_key = "winner_a" if key == "test_a" else "winner_b"
                    if _d in ("BUY", "OPEN_BUY") and _move >= _PRICE_THRESHOLD:
                        entry[_winner_key] = "CORRECT"
                    elif _d in ("SELL", "OPEN_SELL") and _move <= -_PRICE_THRESHOLD:
                        entry[_winner_key] = "CORRECT"
                    elif _d in ("WAIT", "HOLD_TRADE") and abs(_move) < _PRICE_THRESHOLD:
                        entry[_winner_key] = "CORRECT"
                    else:
                        entry[_winner_key] = "INCORRECT"

                _changed = True
                log.info(f"AB_TEST | RESOLVED id={entry.get('id')} | move={_move:+.2f} | "
                         f"A={entry.get('winner_a')} B={entry.get('winner_b')}")

            if _changed:
                _tmp = _ab_path + ".tmp"
                with open(_tmp, "w", encoding="utf-8") as f:
                    json.dump(_results, f, ensure_ascii=False, indent=2)
                os.replace(_tmp, _ab_path)

        except Exception as e:
            log.debug(f"AB_TEST | resolve error: {e}")

    # FLO-262 / FLO-304: Timeframe-to-config mapping for chart screenshots
    _CHART_TF_MAP = {
        "D1":  ("CHART_D1_PNG_PATH",  "d1_ok",  "d1_b64"),
        "H4":  ("CHART_H4_PNG_PATH",  "h4_ok",  "h4_b64"),
        "H1":  ("CHART_H1_PNG_PATH",  "h1_ok",  "h1_b64"),
        "M15": ("CHART_M15_PNG_PATH", "m15_ok", "m15_b64"),
        "M5":  ("CHART_M5_PNG_PATH",  "m5_ok",  "m5_b64"),
        "M1":  ("CHART_M1_PNG_PATH",  "m1_ok",  "m1_b64"),  # FLO-304
    }

    def _request_chart_screenshots(self, timeout: float = 10.0) -> dict:
        """Request chart screenshots from EA and return base64-encoded images."""
        import base64 as _b64

        request_path = getattr(config, 'SCREENSHOT_REQUEST_JSON_PATH', '')
        ready_path = getattr(config, 'SCREENSHOT_READY_JSON_PATH', '')

        if not request_path or not ready_path:
            return {"success": False}

        # Clean stale ready file
        try:
            if os.path.exists(ready_path):
                os.remove(ready_path)
        except Exception:
            pass

        # Write request
        try:
            req = json.dumps({
                "version": 1,
                "timestamp": utc_iso(),  # FLO-286
                "width": getattr(config, 'CHART_SCREENSHOT_WIDTH', 1280),
                "height": getattr(config, 'CHART_SCREENSHOT_HEIGHT', 720),
            })
            _tmp = request_path + ".tmp"
            with open(_tmp, 'w', encoding='utf-8') as f:
                f.write(req)
            os.replace(_tmp, request_path)
        except Exception as e:
            log.warning(f"SCREENSHOT | failed to write request: {e}")
            return {"success": False}

        # Poll for ready file
        t0 = time.time()
        ready = None
        while time.time() - t0 < timeout:
            if os.path.exists(ready_path):
                try:
                    with open(ready_path, 'r', encoding='utf-8') as f:
                        ready = json.load(f)
                    break
                except (json.JSONDecodeError, PermissionError):
                    time.sleep(0.2)
                    continue
            time.sleep(0.3)

        if ready is None:
            latency = time.time() - t0
            log.warning(f"SCREENSHOT | timeout after {latency:.1f}s")
            try:
                if os.path.exists(request_path):
                    os.remove(request_path)
            except Exception:
                pass
            return {"success": False}

        result = {"success": False}

        # FLO-262: Read all available timeframe PNGs
        for tf, (cfg_key, ready_key, b64_key) in self._CHART_TF_MAP.items():
            png_path = getattr(config, cfg_key, '')
            if ready.get(ready_key) and png_path and os.path.exists(png_path):
                try:
                    with open(png_path, 'rb') as f:
                        result[b64_key] = _b64.b64encode(f.read()).decode('ascii')
                except Exception as e:
                    log.warning(f"SCREENSHOT | failed to read {tf}: {e}")

        result["success"] = any(result.get(v[2]) for v in self._CHART_TF_MAP.values())

        # Clean up
        try:
            os.remove(ready_path)
        except Exception:
            pass

        latency = time.time() - t0
        # FLO-262: include D1 and H4 in log when available
        def _kb(key):
            v = result.get(key)
            return len(v) // 1024 if v else 0
        parts = []
        for tf in ("d1", "h4", "h1", "m15", "m5", "m1"):  # FLO-304
            k = f"{tf}_b64"
            parts.append(f"{tf}={'yes' if result.get(k) else 'no'} ({_kb(k)}KB)")
        log.info(f"SCREENSHOT | {' '.join(parts)} | {latency:.1f}s")
        return result

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
                # Compute flip_phase for historical FLIP zones
                flip_phase = ""
                if z.zone_type == "FLIP":
                    flip_phase = "R_TO_S" if z.midpoint <= cp else "S_TO_R"

                # ALWAYS override zone_type based on current price position
                zt = "SUPPORT" if z.midpoint <= cp else "RESISTANCE"
                zones_out.append({
                    "price": round(z.midpoint, 2),
                    "zone_type": zt,
                    "touches": z.touches,
                    "timeframe": z.timeframe,
                    "confluence": z.confluence if z.confluence else [],
                    "strength": z.strength,
                    "position": "above" if z.midpoint > cp else "below",
                    "flip_phase": flip_phase,
                    "volume": int(getattr(z, "volume", 0)),           # FLO-312
                    "volume_bucket": getattr(z, "volume_bucket", "—"),  # FLO-312
                })

            payload = {
                "updated_at": utc_iso(),  # FLO-286: UTC ISO with Z (was LOCAL strftime)
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

    def _write_sr_zones_per_tf_json(self, current_price: float, per_tf: dict):
        """FLO-262: Write separate sr_zones_{tf}.json files for per-TF chart display."""
        try:
            import json
            import os

            cp = current_price
            if not cp:
                return

            base_path = getattr(config, 'SR_ZONES_JSON_PATH', '')
            if not base_path:
                return
            base_dir = os.path.dirname(base_path)

            tf_max_zones = {"D1": 8, "H4": 12, "H1": 8}

            for tf, zones in per_tf.items():
                max_z = tf_max_zones.get(tf, 8)
                # Select nearest zones above + below current price
                above = sorted([z for z in zones if z.midpoint > cp], key=lambda z: z.midpoint)[:max_z // 2]
                below = sorted([z for z in zones if z.midpoint <= cp], key=lambda z: -z.midpoint)[:max_z // 2]

                zones_out = []
                for z in above + below:
                    zt = "SUPPORT" if z.midpoint <= cp else "RESISTANCE"
                    flip_phase = ""
                    if z.zone_type == "FLIP":
                        flip_phase = "R_TO_S" if z.midpoint <= cp else "S_TO_R"
                    zones_out.append({
                        "price": round(z.midpoint, 2),
                        "zone_type": zt,
                        "touches": z.touches,
                        "timeframe": z.timeframe,
                        "confluence": z.confluence if z.confluence else [],
                        "strength": z.strength,
                        "position": "above" if z.midpoint > cp else "below",
                        "flip_phase": flip_phase,
                        "is_confluence": len(z.confluence) > 1,
                        "volume": int(getattr(z, "volume", 0)),           # FLO-312
                        "volume_bucket": getattr(z, "volume_bucket", "—"),  # FLO-312
                    })

                payload = {
                    "updated_at": utc_iso(),  # FLO-286: UTC ISO with Z (was LOCAL strftime)
                    "current_price": round(cp, 2),
                    "timeframe": tf,
                    "zones_count": len(zones_out),
                    "zones": zones_out,
                }

                tf_path = os.path.join(base_dir, f"sr_zones_{tf.lower()}.json")
                with open(tf_path, 'w', encoding='utf-8') as f:
                    json.dump(payload, f, indent=2)

            d1_n = len(per_tf.get("D1", []))
            h4_n = len(per_tf.get("H4", []))
            h1_n = len(per_tf.get("H1", []))
            log.info(f"   S/R per-TF JSON: D1={d1_n} H4={h4_n} H1={h1_n} written")
        except Exception as e:
            log.debug(f"   S/R per-TF JSON error (non-blocking): {e}")

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
                    "echo_classification": None,
                }
                for h in raw_headlines[:8]
            ]

            # Enrich with Echo classifications from echo_alerts.json
            try:
                import json as _json
                _alerts_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "echo_alerts.json")
                if os.path.exists(_alerts_path):
                    with open(_alerts_path, "r", encoding="utf-8") as _af:
                        _echo_alerts = _json.load(_af)
                    if isinstance(_echo_alerts, list):
                        _alert_map = {a.get("title", "").lower()[:50]: a for a in _echo_alerts}
                        # Tag matching headlines
                        for hl in headlines:
                            key = hl["title"].lower()[:50]
                            if key in _alert_map:
                                hl["echo_classification"] = _alert_map[key].get("classification")
                                hl["echo_impact"] = _alert_map[key].get("gold_impact")
                        # Add Echo-only headlines not already in Scanner set
                        seen_keys = {hl["title"].lower()[:50] for hl in headlines}
                        for a in reversed(_echo_alerts[-20:]):
                            if len(headlines) >= 12:
                                break
                            key = (a.get("title") or "").lower()[:50]
                            if key not in seen_keys and a.get("classification") in ("CRITICAL", "IMPORTANT"):
                                headlines.append({
                                    "title": _s(a.get("title", "")),
                                    "score": a.get("relevance_score", 50),
                                    "method": "echo",
                                    "age_hours": 0,
                                    "source": _s(a.get("source", "")),
                                    "category": "echo",
                                    "echo_classification": a.get("classification"),
                                    "echo_impact": a.get("gold_impact"),
                                })
                                seen_keys.add(key)
            except Exception:
                pass

            # Macro components
            dxy_comp = components.get("dxy", {})
            yields_comp = components.get("yields", {})
            vix_comp = components.get("vix", {})

            oil_comp = components.get("oil", {})
            sp500_comp = components.get("sp500", {})

            gld_comp = components.get("gld", {})
            real_yields_comp = components.get("real_yields", {})
            usdcny_comp = components.get("usdcny", {})
            breakeven_comp = components.get("breakeven", {})

            # FLO-76: Real yield intraday proxy
            _ry_proxy = None
            _nom = yields_comp.get("current")
            _be = breakeven_comp.get("current")
            if _nom is not None and _be is not None:
                try:
                    _ry_proxy = round(float(_nom) - float(_be), 2)
                except (TypeError, ValueError):
                    pass

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
                "oil": {
                    "value": oil_comp.get("current"),
                    "change_pct": oil_comp.get("change_percent"),
                },
                "sp500": {
                    "value": sp500_comp.get("current"),
                    "change_pct": sp500_comp.get("change_percent"),
                },
                "gld": {
                    "value": gld_comp.get("volume"),
                    "change_pct": gld_comp.get("change_percent"),
                },
                "real_yields": {
                    "value": real_yields_comp.get("current"),
                    "change_pct": real_yields_comp.get("change"),
                    "proxy": _ry_proxy,
                },
                "usdcny": {
                    "value": usdcny_comp.get("current"),
                    "change_pct": usdcny_comp.get("change_percent"),
                },
            }

            # FLO-77: GLD weekly flows from cached file
            try:
                _gld_flows_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "gld_weekly_flows.json")
                if os.path.exists(_gld_flows_path):
                    with open(_gld_flows_path, "r", encoding="utf-8") as _gf:
                        _gld_flows = json.loads(_gf.read())
                    if isinstance(_gld_flows, dict) and _gld_flows.get("direction"):
                        macro["gld_flows"] = {
                            "direction": _gld_flows.get("direction"),
                            "volume_change_pct": _gld_flows.get("volume_change_pct"),
                            "price_change_pct": _gld_flows.get("price_change_pct"),
                        }
            except Exception:
                pass

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

            market_watch_time = ""
            try:
                if upcoming:
                    rt = upcoming[0].get("reference_time")
                    if isinstance(rt, str) and len(rt) >= 16:
                        market_watch_time = rt[11:16]
            except Exception:
                market_watch_time = ""
            
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
                "market_watch_time": market_watch_time,
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
                be_thr = float(getattr(self, "_breakeven_threshold", 0.50))
                
                if not is_pending:
                    # Real P&L confirmed — count in daily stats
                    self.daily_stats['trades'] += 1
                    if profit >= be_thr:
                        self.daily_stats['wins'] += 1
                    elif profit <= -be_thr:
                        self.daily_stats['losses'] += 1
                    else:
                        self.daily_stats['breakevens'] = self.daily_stats.get('breakevens', 0) + 1
                    self.daily_stats['pnl'] += profit

                    # FLO-78: Discord card for confirmed trade close
                    try:
                        from discord_cards import build_floki_close_card, send_built_card
                        send_built_card(build_floki_close_card(
                            ticket=action.get("ticket", 0),
                            direction=action.get("direction", ""),
                            pnl=profit,
                            entry=action.get("open_price"),
                            exit_price=action.get("close_price"),
                            close_reason=action.get("reason", ""),
                            day_pnl=self.daily_stats.get("pnl"),
                        ))
                    except Exception:
                        pass

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
                try:
                    if isinstance(close_reason, str) and close_reason.strip() == "Stop Loss" and (not is_pending) and profit is not None and float(profit) > 0:
                        close_reason = "Trailing Stop"
                except Exception:
                    pass
                if is_pending:
                    close_reason = f"{close_reason} (pending)"
                # MFE/MAE resolution — three tiers, stop at first hit:
                #   Tier 1 (FLO-269): agent_monitor in-memory tracker
                #   Tier 2 (FLO-276): trade_snapshots table (COALESCE)
                #   Tier 3 (FLO-287): MT5 M1 candle backfill — catches fast trades
                #                     that opened+closed during a Floki cycle, so
                #                     neither Brain nor monitor ever observed them.
                _mfe = None
                _mae = None
                _final_sl = action.get("orig_sl")  # fallback: SL at close from monitor
                try:
                    _t = action.get("ticket")
                    if self._agent_monitor and _t:
                        _mfe = self._agent_monitor.max_profit_seen_points_by_ticket.get(int(_t))
                        _mae = self._agent_monitor.min_profit_seen_points_by_ticket.get(int(_t))
                    # Tier 2: trade_snapshots
                    if _t and (_mfe is None or _mae is None):
                        try:
                            import sqlite3 as _sql
                            _db = os.path.abspath(getattr(config, 'HISTORY_DB_PATH', 'data/history.db'))
                            _c = _sql.connect(_db, timeout=5)
                            _row = _c.execute(
                                'SELECT MAX(profit_pips), MIN(profit_pips) FROM trade_snapshots WHERE ticket = ?',
                                (int(_t),),
                            ).fetchone()
                            _c.close()
                            if _row:
                                if _mfe is None and _row[0] is not None:
                                    _mfe = float(_row[0])
                                if _mae is None and _row[1] is not None:
                                    _mae = float(_row[1])
                        except Exception:
                            pass
                    # Tier 3: MT5 M1 candle backfill (FLO-287)
                    if _t and (_mfe is None or _mae is None):
                        try:
                            from mfe_backfill import backfill_mfe_mae_from_m1
                            # Look up the canonical trade record for entry/times.
                            import sqlite3 as _sql
                            _db = os.path.abspath(getattr(config, 'HISTORY_DB_PATH', 'data/history.db'))
                            _c = _sql.connect(_db, timeout=5)
                            _c.row_factory = _sql.Row
                            _tr = _c.execute(
                                "SELECT direction, open_price, open_time, close_time FROM trades WHERE ticket = ?",
                                (int(_t),),
                            ).fetchone()
                            _c.close()
                            if _tr:
                                _ct = action.get('close_time') or _tr['close_time']
                                _bf_mfe, _bf_mae = backfill_mfe_mae_from_m1(
                                    ticket=int(_t),
                                    direction=_tr['direction'] or action.get('direction'),
                                    entry=float(_tr['open_price']) if _tr['open_price'] is not None else None,
                                    open_iso=_tr['open_time'],
                                    close_iso=_ct,
                                )
                                if _mfe is None and _bf_mfe is not None:
                                    _mfe = _bf_mfe
                                if _mae is None and _bf_mae is not None:
                                    _mae = _bf_mae
                        except Exception as _e:
                            log.debug(f"MFE backfill tier-3 error (ignored): {_e}")
                    # final_sl: use the last known SL from position_monitor (more accurate)
                    if hasattr(self, '_position_monitor') and self._position_monitor and _t:
                        _tsl = self._position_monitor.trailing_sl.get(int(_t))
                        if _tsl is not None:
                            _final_sl = _tsl
                except Exception:
                    pass
                record_trade_close(
                    ticket=action.get("ticket"),
                    close_price=action.get("close_price"),
                    profit=profit if not is_pending else None,
                    close_reason=close_reason,
                    close_time=action.get("close_time"),
                    breakeven_activated=action.get("breakeven_activated", False),
                    mfe_points=_mfe,
                    mae_points=_mae,
                    final_sl=_final_sl,
                )

                # Update L2 pattern memory after a confirmed close (non-blocking)
                if not is_pending:
                    try:
                        run_reflection_async("trade_close")
                    except Exception:
                        pass

                    # FLO-68: Sage intraday drawdown check after every confirmed close
                    try:
                        from sage_auditor import check_intraday_drawdown
                        check_intraday_drawdown()
                    except Exception:
                        pass

                    # FLO-63: Extract trade lesson after confirmed close
                    try:
                        from trade_lessons import extract_trade_lesson
                        extract_trade_lesson(action.get("ticket"))
                    except Exception:
                        pass

                    # FLO-137: Post-trade reflexion (GPT-5.4, daemon thread)
                    try:
                        from trade_reflexion import run_trade_reflexion_async, schedule_delayed_hindsight
                        run_trade_reflexion_async(action)
                        schedule_delayed_hindsight(action)
                    except Exception:
                        pass

                    # FLO-269: Generate hard-data post-trade report
                    try:
                        from trade_reflexion import generate_post_trade_report
                        generate_post_trade_report(action)
                    except Exception:
                        pass
                
                # Record for safety checks (cooldown applies even for pending)
                record_trade_result(profit)
                
                # Record close type for dynamic cooldown
                close_type = action.get('close_type', 'sl')
                trade_dir = action.get('direction', 'BUY')
                record_close_type(trade_dir, close_type, pnl=profit)
        
        # Persist state after monitor cycle
        write_state(self)
    
    def _check_daily_reset(self):
        """Check and reset daily statistics"""
        today = trading_day_broker_aligned()  # FLO-286: broker-midnight aligned

        if today != str(self.daily_stats['date']):
            # Rotate log file to new day BEFORE anything else logs
            log.rotate_if_needed()

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
    from discord_cards import send_card, COLORS
    result = send_card("floki", COLORS["system"], "\u2699\uFE0F SYSTEM", "\U0001F916 Connection test", description="FlokiWatch bot connection test")
    print(f"   Result: {'✅ OK' if result else '❌ Failed'}")


# ============================================================================
# MAIN
# ============================================================================

_PID_LOCK_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "bot.pid")


def _acquire_pid_lock() -> bool:
    """Ensure only one bot instance runs at a time (process-level singleton).

    Writes current PID to data/bot.pid.  If the file already exists and the
    PID inside is still alive, refuse to start.  Returns True if lock acquired.
    """
    pid_path = _PID_LOCK_PATH
    os.makedirs(os.path.dirname(pid_path), exist_ok=True)

    if os.path.exists(pid_path):
        try:
            with open(pid_path, "r") as f:
                old_pid = int(f.read().strip())
            # Check if old process is still alive
            import ctypes
            kernel32 = ctypes.windll.kernel32
            SYNCHRONIZE = 0x00100000
            handle = kernel32.OpenProcess(SYNCHRONIZE, False, old_pid)
            if handle:
                kernel32.CloseHandle(handle)
                print(f"FATAL: Another bot instance is already running (PID {old_pid}).")
                print(f"Kill it first:  taskkill /PID {old_pid} /F")
                return False
        except (ValueError, OSError, AttributeError):
            pass  # stale/corrupt pid file or non-Windows — safe to proceed

    # Write our PID
    try:
        with open(pid_path, "w") as f:
            f.write(str(os.getpid()))
    except Exception:
        pass
    return True


def _release_pid_lock():
    """Remove PID lockfile on exit."""
    try:
        if os.path.exists(_PID_LOCK_PATH):
            with open(_PID_LOCK_PATH, "r") as f:
                stored_pid = int(f.read().strip())
            if stored_pid == os.getpid():
                os.remove(_PID_LOCK_PATH)
    except Exception:
        pass


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
        # Singleton guard — prevent two bot instances from running
        if not _acquire_pid_lock():
            sys.exit(1)
        import atexit
        atexit.register(_release_pid_lock)

        # Run bot
        bot = TradingBot()
        bot.run()

if __name__ == "__main__":
    main()
