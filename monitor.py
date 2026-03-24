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
    
    def __init__(self, bot=None):
        self.bot = bot
        # Track positions that already hit breakeven
        self.breakeven_hit_tickets = set()
        # Track whether BE was ever activated for each ticket (persists until closure)
        self.breakeven_activated_tickets = {}
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
        # Track last known SL per ticket (for EA-side change detection)
        self.last_known_sl = {}
        # Track account balance at trade open (captured when ticket is first detected in MT5)
        self.balance_at_open = {}

    def _agent_monitor_events_path(self) -> str:
        import os

        base_dir = os.path.dirname(os.path.abspath(__file__))
        data_dir = os.path.join(base_dir, "data")
        os.makedirs(data_dir, exist_ok=True)
        return os.path.join(data_dir, "agent_monitor_events.json")

    def _load_agent_monitor_events(self) -> List[dict]:
        import json
        import os

        path = self._agent_monitor_events_path()
        if not os.path.exists(path):
            return []
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, list) else []
        except Exception:
            return []

    def _save_agent_monitor_events(self, events: List[dict]) -> bool:
        import json
        import os

        path = self._agent_monitor_events_path()
        try:
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(events, f, ensure_ascii=False, indent=2)
            if os.path.exists(path):
                os.remove(path)
            os.rename(tmp, path)
            return True
        except Exception:
            return False

    def _append_agent_monitor_event(self, event: str, ticket: int, details: str = "") -> None:
        try:
            payload = {
                "timestamp": datetime.utcnow().isoformat(timespec="seconds") + "Z",
                "event": str(event or "").strip(),
                "ticket": int(ticket),
                "details": str(details or "").strip(),
            }

            events = self._load_agent_monitor_events()
            events.append(payload)
            events = events[-20:]
            self._save_agent_monitor_events(events)
        except Exception:
            return
    
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

        # Agent-defined watch conditions (checked locally once/min when market is open)
        try:
            watch_actions = self._check_agent_watch_conditions(positions, current_tickets)
            if watch_actions:
                actions.extend(watch_actions)
        except Exception as e:
            log.debug(f"   Monitor: watch conditions error (non-blocking): {e}")
        
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
                log.info(f"   Monitor: #{pos.ticket} Triggers: BE={be_trig:.0f} pips, Trail={tr_trig:.0f} pips, Dist={tr_dist:.0f} pips)")

                # Capture balance at trade open (PRIMARY reference for real P&L on broker close)
                try:
                    account_info = executor.get_account_info()
                    if account_info and account_info.get('balance') is not None:
                        bal = float(account_info['balance'])
                        self.balance_at_open[pos.ticket] = bal
                        source = "startup_first_sight" if not self._initialized else "first_sight"
                        log.info(
                            f"BALANCE_CAPTURE | ticket=#{pos.ticket} | balance=${bal:.2f} | source={source}"
                        )
                    else:
                        source = "startup_first_sight" if not self._initialized else "first_sight"
                        log.warning(
                            f"BALANCE_CAPTURE | WARNING | ticket=#{pos.ticket} | balance_unavailable | source={source}"
                        )
                except Exception as e:
                    log.debug(f"   Monitor: balance capture error (non-blocking): {e}")

                # Remap Agent watch conditions from placeholder ticket=0 to real MT5 ticket on first sight
                try:
                    watch = self._load_watch_conditions()
                    if isinstance(watch, dict) and "0" in watch and str(pos.ticket) not in watch:
                        payload0 = watch.get("0")
                        conds0 = payload0.get("conditions") if isinstance(payload0, dict) else None
                        if isinstance(conds0, list) and conds0:
                            watch[str(pos.ticket)] = payload0
                            try:
                                del watch["0"]
                            except Exception:
                                pass
                            self._save_watch_conditions(watch)
                            log.info(f"WATCH_REMAP | 0 -> #{pos.ticket} | count={len(conds0)}")
                except Exception as e:
                    log.debug(f"   Monitor: watch remap error (non-blocking): {e}")
                
                # Update DB with actual MT5 fill price (EA Bridge path records ticket=0 initially)
                try:
                    from db_writer import update_trade_open_price
                    update_trade_open_price(
                        new_ticket=pos.ticket,
                        direction=pos.direction,
                        actual_open_price=pos.open_price,
                    )
                except Exception as e:
                    log.warning(f"Monitor: update_trade_open_price failed for #{pos.ticket}: {e}")

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
        
        # Detect EA-side SL changes (trailing stop moved by EA, not Python)
        for pos in positions:
            ticket = pos.ticket
            current_sl = pos.sl
            
            if ticket in self.last_known_sl:
                last_sl = self.last_known_sl[ticket]
                # SL changed since last cycle?
                if abs(current_sl - last_sl) > 0.01:  # >0.01 to avoid float noise
                    # Check if Python made this change (trailing_sl would match)
                    python_sl = self.trailing_sl.get(ticket)
                    if python_sl is None or abs(current_sl - python_sl) > 0.01:
                        # EA-side change detected — send alert
                        log.info(f"   Monitor: EA trailing detected #{ticket} SL: {last_sl:.2f} → {current_sl:.2f}")
                        alert_trailing_stop(
                            ticket,
                            last_sl,
                            current_sl,
                            direction=pos.direction,
                            entry_price=pos.open_price,
                            profit_pips=pos.profit_pips,
                        )
            
            # Update last known SL for next cycle
            self.last_known_sl[ticket] = current_sl
        
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

    def _watch_conditions_path(self) -> str:
        import os

        base_dir = os.path.dirname(os.path.abspath(__file__))
        data_dir = os.path.join(base_dir, "data")
        os.makedirs(data_dir, exist_ok=True)
        return os.path.join(data_dir, "agent_watch_conditions.json")

    def _load_watch_conditions(self) -> dict:
        import json
        import os

        path = self._watch_conditions_path()
        if not os.path.exists(path):
            return {}
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _save_watch_conditions(self, payload: dict) -> bool:
        import json
        import os

        path = self._watch_conditions_path()
        try:
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            if os.path.exists(path):
                os.remove(path)
            os.rename(tmp, path)
            return True
        except Exception:
            return False

    def _check_agent_watch_conditions(self, positions: List[PositionInfo], current_tickets: set) -> List[dict]:
        from safety_checks import is_market_open

        is_open, _, _ = is_market_open()
        if not is_open:
            return []

        watch = self._load_watch_conditions()
        if not watch:
            return []

        # Only evaluate once per minute
        now = datetime.utcnow()
        try:
            last_ts = getattr(self, "_last_watch_eval_ts", None)
            if last_ts and (now - last_ts).total_seconds() < 60:
                return []
            setattr(self, "_last_watch_eval_ts", now)
        except Exception:
            pass

        # Clear stale tickets that are no longer open
        try:
            open_ticket_strs = {str(t) for t in current_tickets}
            stale_keys = [k for k in list(watch.keys()) if str(k) not in open_ticket_strs]
            if stale_keys:
                for k in stale_keys:
                    try:
                        del watch[k]
                    except Exception:
                        pass
                self._save_watch_conditions(watch)
        except Exception:
            pass

        pos_by_ticket = {p.ticket: p for p in positions}
        actions: List[dict] = []

        for ticket_str, payload in list(watch.items()):
            try:
                t = int(ticket_str)
            except Exception:
                continue

            pos = pos_by_ticket.get(t)
            if pos is None:
                continue

            conds = payload.get("conditions") if isinstance(payload, dict) else None
            if not isinstance(conds, list) or not conds:
                continue

            triggered = None
            trigger_reason = None

            current_price = float(pos.current_price)
            pnl = float(pos.profit)

            for c in conds:
                if not isinstance(c, dict):
                    continue
                ctype = str(c.get("type", "")).strip()
                desc = str(c.get("description", "")).strip()

                if ctype == "price_touch":
                    try:
                        lvl = float(c.get("level"))
                    except Exception:
                        continue
                    # Tolerance: 0.5 pips
                    if abs(current_price - lvl) <= 0.05:
                        triggered = c
                        trigger_reason = desc or f"price_touch {lvl}"
                        break

                elif ctype == "pnl_threshold":
                    try:
                        thr = float(c.get("value"))
                    except Exception:
                        continue
                    if (thr < 0 and pnl <= thr) or (thr > 0 and pnl >= thr) or thr == 0:
                        triggered = c
                        trigger_reason = desc or f"pnl_threshold {thr}"
                        break

                elif ctype == "indicator_threshold":
                    # v1: VIX only, pulled from cached macro if available
                    ind = str(c.get("indicator", "")).strip().lower()
                    direction = str(c.get("direction", "")).strip().lower()
                    try:
                        lvl = float(c.get("level"))
                    except Exception:
                        continue
                    if ind != "vix" or direction not in ("above", "below"):
                        continue

                    vix_val = None
                    try:
                        vix_val = getattr(getattr(self, "bot", None), "_last_agent_data", {}).get("macro_data", {}).get("vix")
                    except Exception:
                        vix_val = None
                    try:
                        vix_f = float(vix_val) if vix_val is not None else None
                    except Exception:
                        vix_f = None
                    if vix_f is None:
                        continue

                    if (direction == "above" and vix_f >= lvl) or (direction == "below" and vix_f <= lvl):
                        triggered = c
                        trigger_reason = desc or f"vix {direction} {lvl}"
                        break

            if not triggered:
                continue

            # Call Agent with watch-trigger context (non-blocking; ignore failures)
            try:
                pass
            except Exception as e:
                log.debug(f"   Monitor: watch trigger agent call failed (ignored): {e}")

            # Clear watch conditions for this ticket after first trigger
            try:
                del watch[ticket_str]
                self._save_watch_conditions(watch)
            except Exception:
                pass

            actions.append({"action": "WATCH_TRIGGER", "ticket": t, "reason": trigger_reason})

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
            self.breakeven_activated_tickets[pos.ticket] = True
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
            alert_breakeven(
                pos.ticket,
                pos.sl,
                breakeven_sl,
                pos.profit_pips,
                direction=pos.direction,
                entry_price=pos.open_price,
            )

            self._append_agent_monitor_event(
                "BREAKEVEN_ACTIVATED",
                pos.ticket,
                details=f"SL moved to {breakeven_sl:.2f} (entry) from {pos.sl:.2f} at profit {pos.profit_pips:+.0f} pips",
            )
            
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
            alert_trailing_stop(
                pos.ticket,
                old_sl,
                new_sl,
                direction=pos.direction,
                entry_price=pos.open_price,
                profit_pips=pos.profit_pips,
            )

            self._append_agent_monitor_event(
                "TRAILING_UPDATED",
                pos.ticket,
                details=f"SL moved {old_sl:.2f} -> {new_sl:.2f} at profit {pos.profit_pips:+.0f} pips",
            )
            
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

            self._append_agent_monitor_event(
                "TIMEOUT_CLOSE",
                pos.ticket,
                details=f"Closed by timeout after {time_open} | profit={pos.profit:+.2f} ({pos.profit_pips:+.0f} pips)",
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

            self._append_agent_monitor_event(
                "DRAWDOWN_CLOSE",
                pos.ticket,
                details=f"Closed by drawdown | profit={pos.profit:+.2f} ({pos.profit_pips:+.0f} pips)",
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

            # PRIMARY P&L source of truth: balance diff (single-position invariant)
            profit_balance = None
            try:
                bal_open = self.balance_at_open.get(ticket)
                account_info_now = executor.get_account_info()
                bal_now = float(account_info_now['balance']) if account_info_now and account_info_now.get('balance') is not None else None
                if bal_open is not None and bal_now is not None:
                    profit_balance = bal_now - float(bal_open)
                    log.info(
                        f"BALANCE_DIFF | ticket=#{ticket} | open=${float(bal_open):.2f} | now=${float(bal_now):.2f} | diff=${float(profit_balance):+.2f}"
                    )
                elif bal_open is None:
                    log.warning(f"BALANCE_CAPTURE | WARNING | no balance_at_open for ticket #{ticket}")
                elif bal_now is None:
                    log.warning(f"BALANCE_DIFF | WARNING | ticket=#{ticket} | balance_now_unavailable")
            except Exception as e:
                log.debug(f"   Monitor: balance diff error (non-blocking): {e}")
            
            deal = get_deal_history(ticket, open_price=pos.open_price, tp_price=pos.tp, sl_price=pos.sl)

            deal_is_pending = bool(deal and (deal.get('pending') or deal.get('estimated')))
            if deal is None or deal_is_pending:
                log.info(f"DEAL_REFRESH | Forcing MT5 reconnect for ticket #{ticket}")
                try:
                    executor.disconnect()
                    executor.connect()
                except Exception as e:
                    log.debug(f"   Monitor: MT5 reconnect failed (non-blocking): {e}")

                deal_after_refresh = get_deal_history(ticket, open_price=pos.open_price, tp_price=pos.tp, sl_price=pos.sl)
                if deal_after_refresh is not None and not (deal_after_refresh.get('pending') or deal_after_refresh.get('estimated')):
                    deal = deal_after_refresh
                elif deal is None:
                    deal = deal_after_refresh
            
            if deal:
                # We have close details
                is_pending = deal.get('pending', False)
                profit = profit_balance if profit_balance is not None else (deal['profit'] if not is_pending else 0)
                reason = deal['reason']
                direction = deal['direction']
                close_price = deal['close_price']
                outcome = deal.get('outcome')

                if profit_balance is not None:
                    is_pending = False
                    try:
                        deal_profit = deal.get('profit')
                        if deal_profit is not None:
                            drift = abs(float(deal_profit) - float(profit_balance))
                            if drift > 0.05:
                                log.warning(
                                    f"   Monitor: P&L drift for #{ticket}: balance_diff=${profit_balance:+.2f} vs deal=${float(deal_profit):+.2f}"
                                )
                    except Exception:
                        pass
                
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

                if not is_pending:
                    try:
                        self._append_agent_monitor_event(
                            event="BROKER_CLOSE",
                            ticket=ticket,
                            details=f"reason={reason} | profit=${profit:+.2f} | close_price={close_price:.2f}",
                        )
                    except Exception:
                        pass
                
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
                    'breakeven_activated': self.breakeven_activated_tickets.get(ticket, False),
                })
            else:
                # No details — notify anyway
                log.position_update(
                    ticket, "BROKER_CLOSE",
                    f"Position closed (no details in history)"
                )

                profit = profit_balance if profit_balance is not None else 0
                is_pending = False if profit_balance is not None else True
                account_info = executor.get_account_info()
                balance = account_info['balance'] if account_info else config.CAPITAL_INICIAL
                profit_percent = (profit / balance) * 100 if not is_pending else 0
                
                alert_trade_closed(
                    ticket=ticket,
                    direction=pos.direction,
                    profit=profit,
                    profit_percent=profit_percent,
                    reason="Closed by broker (details unavailable)",
                    pending=is_pending,
                )

                if not is_pending:
                    try:
                        self._append_agent_monitor_event(
                            event="BROKER_CLOSE",
                            ticket=ticket,
                            details=f"reason=details_unavailable | profit=${profit:+.2f}",
                        )
                    except Exception:
                        pass
                
                actions.append({
                    'action': 'BROKER_CLOSE',
                    'ticket': ticket,
                    'volume': pos.volume,
                    'open_price': pos.open_price,
                    'close_price': None,
                    'profit': profit,
                    'reason': 'unknown',
                    'close_time': None,
                    'close_type': 'sl',  # conservative default
                    'direction': pos.direction,
                    'pending': is_pending,
                    'breakeven_activated': self.breakeven_activated_tickets.get(ticket, False),
                })
            
            # Clean up tracking
            self._cleanup_position(ticket)
        
        return actions
    
    def _cleanup_position(self, ticket: int):
        """Clean up tracking for closed position"""
        self.breakeven_hit_tickets.discard(ticket)
        self.breakeven_activated_tickets.pop(ticket, None)
        self.trailing_sl.pop(ticket, None)
        self.original_sl_pips.pop(ticket, None)
        self.balance_at_open.pop(ticket, None)
    
    def get_position_phase(self, ticket: int) -> str:
        """
        Get the current phase of a position.
        
        Returns:
            "OPEN" - Position has not hit breakeven
            "BREAKEVEN" - SL moved to entry, trailing not yet active
            "TRAILING" - Trailing stop is active
        """
        if ticket in self.trailing_sl:
            return "TRAILING"
        elif ticket in self.breakeven_hit_tickets:
            return "BREAKEVEN"
        else:
            return "OPEN"
    
    def get_be_info(self, ticket: int) -> dict:
        """
        Get breakeven info for a position (for dashboard display).
        
        Returns:
            dict with be_trigger_pips, be_remaining_pips, be_activated
        """
        pos = self.known_positions.get(ticket)
        if not pos:
            return {"be_trigger_pips": None, "be_remaining_pips": None, "be_activated": False}
        
        be_activated = ticket in self.breakeven_hit_tickets
        
        if be_activated:
            return {
                "be_trigger_pips": 0,
                "be_remaining_pips": 0,
                "be_activated": True
            }
        
        # Calculate trigger
        if self.volatility_status == "COOLING_DOWN":
            trigger = config.COOLING_BREAKEVEN_TRIGGER_PIPS
        else:
            sl_pips = self._get_original_sl_pips(pos)
            if sl_pips > 0:
                trigger = sl_pips * getattr(config, 'BREAKEVEN_ATR_MULT', 0.7)
            else:
                trigger = config.BREAKEVEN_TRIGGER_PIPS
        
        remaining = max(0, trigger - pos.profit_pips)
        
        return {
            "be_trigger_pips": round(trigger, 1),
            "be_remaining_pips": round(remaining, 1),
            "be_activated": False
        }
    
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
                    'open_time': p.open_time.isoformat(),
                    'phase': self.get_position_phase(p.ticket)
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
