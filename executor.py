"""
ORDER EXECUTOR - MT5 Order Execution
Automatically sends orders to MetaTrader 5
"""

import MetaTrader5 as mt5
import time
from dataclasses import dataclass
from typing import Optional, Tuple, List
from datetime import datetime, timedelta
import config
from logger import log
from alerts import (
    alert_trade_executed, alert_trade_closed, 
    alert_error, alert_trailing_stop
)


@dataclass
class OrderResult:
    """Order result"""
    success: bool
    ticket: Optional[int]
    error_code: Optional[int]
    error_message: Optional[str]
    price: Optional[float]
    volume: Optional[float]


@dataclass
class PositionInfo:
    """Open position information"""
    ticket: int
    symbol: str
    direction: str  # "BUY" or "SELL"
    volume: float
    open_price: float
    current_price: float
    sl: float
    tp: float
    profit: float
    profit_pips: float
    open_time: datetime
    magic: int
    comment: str


class MT5Executor:
    """MT5 order executor"""
    
    def __init__(self):
        self.connected = False
        self.symbol = config.SYMBOL
        self.magic = config.MAGIC_NUMBER
        self.dry_run = config.DRY_RUN
    
    def connect(self) -> bool:
        """Connect to MT5"""
        terminal_path = getattr(config, 'MT5_TERMINAL_PATH', None)
        if terminal_path:
            init_ok = mt5.initialize(path=terminal_path)
        else:
            init_ok = mt5.initialize()
        if not init_ok:
            log.error(f"MT5 initialize failed: {mt5.last_error()}")
            return False
        
        # Log which terminal we connected to
        try:
            ti = mt5.terminal_info()
            if ti:
                log.info(f"MT5 terminal: {ti.path}")
                log.info(f"MT5 data path: {ti.data_path}")
        except Exception:
            pass
        
        # Check if already logged into correct account
        account = mt5.account_info()
        if account and account.login == config.MT5_ACCOUNT:
            log.info(f"MT5 already logged into account {account.login} - skip login")
            self.connected = True
            log.mt5_status(True, f"Connected to account {account.login}")
            return True
        
        # Login if credentials provided
        if config.MT5_ACCOUNT and config.MT5_PASSWORD:
            authorized = mt5.login(
                config.MT5_ACCOUNT,
                password=config.MT5_PASSWORD,
                server=config.MT5_SERVER
            )
            if not authorized:
                log.error(f"MT5 login failed: {mt5.last_error()}")
                return False
        
        self.connected = True
        log.mt5_status(True, f"Connected to account {mt5.account_info().login}")
        return True
    
    def disconnect(self):
        """Disconnect from MT5"""
        mt5.shutdown()
        self.connected = False
        log.mt5_status(False, "Disconnected")
    
    def is_connected(self) -> bool:
        """Check if connected"""
        if not self.connected:
            return False
        
        # Check if still active
        account = mt5.account_info()
        if account is None:
            self.connected = False
            return False
        
        return True
    
    def get_account_info(self) -> Optional[dict]:
        """Return account information"""
        if not self.is_connected():
            return None
        
        account = mt5.account_info()
        if account is None:
            return None
        
        return {
            'login': account.login,
            'balance': account.balance,
            'equity': account.equity,
            'margin': account.margin,
            'free_margin': account.margin_free,
            'profit': account.profit,
            'leverage': account.leverage,
            'currency': account.currency
        }
    
    def get_current_price(self) -> Optional[Tuple[float, float]]:
        """Return current price (bid, ask)"""
        tick = mt5.symbol_info_tick(self.symbol)
        if tick is None:
            return None
        return tick.bid, tick.ask
    
    def get_spread(self) -> Optional[float]:
        """Return current spread in pips"""
        prices = self.get_current_price()
        if prices is None:
            return None
        bid, ask = prices
        return (ask - bid) / 0.1  # XAU/USD: 1 pip = 0.1
    
    def execute_trade(
        self,
        direction: str,
        lot_size: float,
        stop_loss: float,
        take_profit: float,
        comment: str = "",
        confidence: Optional[float] = None,
        scenario: Optional[str] = None,
        risk_amount: Optional[float] = None,
        risk_percent: Optional[float] = None,
    ) -> OrderResult:
        """
        Execute a trade.
        
        Args:
            direction: "BUY" or "SELL"
            lot_size: Lot size
            stop_loss: SL price
            take_profit: TP price
            comment: Order comment
        
        Returns:
            OrderResult with details
        """
        # DRY RUN mode
        if self.dry_run:
            prices = self.get_current_price()
            price = prices[1] if direction == "BUY" else prices[0] if prices else 0
            
            log.trade(f"[DRY RUN] {direction} | Lot:{lot_size} Price:{price:.2f} SL:{stop_loss:.2f} TP:{take_profit:.2f}")
            
            alert_trade_executed(
                direction=direction,
                ticket=999999,
                lot_size=lot_size,
                entry_price=price,
                stop_loss=stop_loss,
                take_profit=take_profit,
                is_dry_run=True,
                confidence=confidence,
                scenario=scenario,
                risk_amount=risk_amount,
                risk_percent=risk_percent,
            )
            
            return OrderResult(
                success=True,
                ticket=999999,
                error_code=None,
                error_message=None,
                price=price,
                volume=lot_size
            )
        
        # Check connection
        if not self.is_connected():
            return OrderResult(
                success=False,
                ticket=None,
                error_code=-1,
                error_message="MT5 not connected",
                price=None,
                volume=None
            )
        
        # Get current price
        prices = self.get_current_price()
        if prices is None:
            return OrderResult(
                success=False,
                ticket=None,
                error_code=-2,
                error_message="Could not get price",
                price=None,
                volume=None
            )
        
        bid, ask = prices
        
        # Configure order
        if direction.upper() == "BUY":
            order_type = mt5.ORDER_TYPE_BUY
            price = ask
        else:
            order_type = mt5.ORDER_TYPE_SELL
            price = bid
        
        # Create request
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": self.symbol,
            "volume": lot_size,
            "type": order_type,
            "price": price,
            "sl": stop_loss,
            "tp": take_profit,
            "deviation": config.MAX_SLIPPAGE_PIPS,
            "magic": self.magic,
            "comment": comment or f"Bot-{direction}",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }
        
        # Send order
        result = mt5.order_send(request)
        
        if result is None:
            error = mt5.last_error()
            log.error(f"Order send failed: {error}")
            alert_error("Order Failed", f"Error sending order: {error}")
            
            return OrderResult(
                success=False,
                ticket=None,
                error_code=error[0] if error else -3,
                error_message=str(error),
                price=None,
                volume=None
            )
        
        # Check result
        if result.retcode != mt5.TRADE_RETCODE_DONE:
            error_msg = self._get_error_message(result.retcode)
            log.error(f"Order rejected: {result.retcode} - {error_msg}")
            alert_error("Order Rejected", f"Code: {result.retcode} - {error_msg}")
            
            return OrderResult(
                success=False,
                ticket=None,
                error_code=result.retcode,
                error_message=error_msg,
                price=None,
                volume=None
            )
        
        # Success - log with spread info
        spread = self.get_spread()
        spread_str = f"{spread:.1f} pips" if spread else "N/A"
        log.order(direction, result.order, lot_size, result.price, stop_loss, take_profit)
        log.success(f"Order executed: Ticket {result.order} | Spread: {spread_str}")
        
        alert_trade_executed(
            direction=direction,
            ticket=result.order,
            lot_size=lot_size,
            entry_price=result.price,
            stop_loss=stop_loss,
            take_profit=take_profit,
            is_dry_run=False,
            confidence=confidence,
            scenario=scenario,
            risk_amount=risk_amount,
            risk_percent=risk_percent,
        )
        
        return OrderResult(
            success=True,
            ticket=result.order,
            error_code=None,
            error_message=None,
            price=result.price,
            volume=result.volume
        )
    
    def get_open_positions(self) -> List[PositionInfo]:
        """Return list of bot's open positions"""
        if not self.is_connected():
            return []
        
        positions = mt5.positions_get(symbol=self.symbol)
        if positions is None:
            return []
        
        result = []
        for pos in positions:
            # Filter only bot positions
            if pos.magic != self.magic:
                continue
            
            # Calculate profit in pips
            pip_size = 0.1
            if pos.type == mt5.POSITION_TYPE_BUY:
                direction = "BUY"
                profit_pips = (pos.price_current - pos.price_open) / pip_size
            else:
                direction = "SELL"
                profit_pips = (pos.price_open - pos.price_current) / pip_size
            
            result.append(PositionInfo(
                ticket=pos.ticket,
                symbol=pos.symbol,
                direction=direction,
                volume=pos.volume,
                open_price=pos.price_open,
                current_price=pos.price_current,
                sl=pos.sl,
                tp=pos.tp,
                profit=pos.profit,
                profit_pips=profit_pips,
                open_time=datetime.fromtimestamp(pos.time),
                magic=pos.magic,
                comment=pos.comment
            ))
        
        return result
    
    def close_position(
        self,
        ticket: int,
        volume: Optional[float] = None
    ) -> OrderResult:
        """
        Close a position (total or partial).
        
        Args:
            ticket: Position ticket
            volume: Volume to close (None = total)
        
        Returns:
            OrderResult
        """
        if self.dry_run:
            log.trade(f"[DRY RUN] CLOSE | Ticket:{ticket} Volume:{volume or 'TOTAL'}")
            return OrderResult(
                success=True,
                ticket=ticket,
                error_code=None,
                error_message=None,
                price=None,
                volume=volume
            )
        
        if not self.is_connected():
            return OrderResult(
                success=False,
                ticket=None,
                error_code=-1,
                error_message="MT5 not connected",
                price=None,
                volume=None
            )
        
        # Get position
        positions = mt5.positions_get(ticket=ticket)
        if not positions:
            return OrderResult(
                success=False,
                ticket=None,
                error_code=-4,
                error_message=f"Position {ticket} not found",
                price=None,
                volume=None
            )
        
        position = positions[0]
        close_volume = volume if volume else position.volume
        
        # Determine closing order type
        if position.type == mt5.POSITION_TYPE_BUY:
            order_type = mt5.ORDER_TYPE_SELL
            price = mt5.symbol_info_tick(self.symbol).bid
        else:
            order_type = mt5.ORDER_TYPE_BUY
            price = mt5.symbol_info_tick(self.symbol).ask
        
        # Create request
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": self.symbol,
            "volume": close_volume,
            "type": order_type,
            "position": ticket,
            "price": price,
            "deviation": config.MAX_SLIPPAGE_PIPS,
            "magic": self.magic,
            "comment": "Bot-Close",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }
        
        result = mt5.order_send(request)
        
        if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
            error_msg = self._get_error_message(result.retcode if result else -1)
            log.error(f"Close position failed: {error_msg}")
            return OrderResult(
                success=False,
                ticket=None,
                error_code=result.retcode if result else -1,
                error_message=error_msg,
                price=None,
                volume=None
            )
        
        log.position_update(ticket, "CLOSED", f"Volume: {close_volume}")
        
        return OrderResult(
            success=True,
            ticket=result.order,
            error_code=None,
            error_message=None,
            price=result.price,
            volume=close_volume
        )
    
    def modify_position(
        self,
        ticket: int,
        new_sl: Optional[float] = None,
        new_tp: Optional[float] = None
    ) -> OrderResult:
        """
        Modify SL/TP of a position.
        
        Args:
            ticket: Position ticket
            new_sl: New SL (None = keep)
            new_tp: New TP (None = keep)
        
        Returns:
            OrderResult
        """
        if self.dry_run:
            log.trade(f"[DRY RUN] MODIFY | Ticket:{ticket} SL:{new_sl} TP:{new_tp}")
            return OrderResult(
                success=True,
                ticket=ticket,
                error_code=None,
                error_message=None,
                price=None,
                volume=None
            )
        
        if not self.is_connected():
            return OrderResult(
                success=False,
                ticket=None,
                error_code=-1,
                error_message="MT5 not connected",
                price=None,
                volume=None
            )
        
        # Get current position
        positions = mt5.positions_get(ticket=ticket)
        if not positions:
            return OrderResult(
                success=False,
                ticket=None,
                error_code=-4,
                error_message=f"Position {ticket} not found",
                price=None,
                volume=None
            )
        
        position = positions[0]
        
        # Use current values if not specified
        sl = new_sl if new_sl is not None else position.sl
        tp = new_tp if new_tp is not None else position.tp
        
        # Create request
        request = {
            "action": mt5.TRADE_ACTION_SLTP,
            "symbol": self.symbol,
            "position": ticket,
            "sl": sl,
            "tp": tp,
        }
        
        result = mt5.order_send(request)
        
        if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
            error_msg = self._get_error_message(result.retcode if result else -1)
            log.error(f"Modify position failed: {error_msg}")
            return OrderResult(
                success=False,
                ticket=None,
                error_code=result.retcode if result else -1,
                error_message=error_msg,
                price=None,
                volume=None
            )
        
        log.position_update(ticket, "MODIFIED", f"SL:{sl:.2f} TP:{tp:.2f}")
        
        return OrderResult(
            success=True,
            ticket=ticket,
            error_code=None,
            error_message=None,
            price=None,
            volume=None
        )
    
    def get_deal_history(self, position_ticket: int, open_price: float = None, tp_price: float = None, sl_price: float = None) -> Optional[dict]:
        """
        Query deal history for a closed position.
        
        Search strategy (3 levels):
        1. history_deals_get(position=ticket) + position_id filter
        2. Broad search: ALL XAUUSD deals + position_id filter
        3. P&L estimation from current price vs SL/TP
        
        If no deal found in levels 1+2, retry with configurable backoff.
        
        Args:
            position_ticket: Position ticket
            open_price: Position open price (for validation)
            tp_price: TP price (for last-resort estimation)
            sl_price: Current SL price (after trailing, for estimation)
            
        Returns:
            Dict with close details, or None if not found
        """
        if not self.is_connected():
            return None
        
        try:
            retry_delays = [0] + list(config.DEAL_HISTORY_RETRY_DELAYS)
            total_attempts = len(retry_delays)
            
            for attempt, delay in enumerate(retry_delays):
                if delay > 0:
                    log.info(f"Deal history: Attempt {attempt + 1}/{total_attempts} — waiting {delay}s for deal to appear in history...")
                    time.sleep(delay)
                
                # === LEVEL 1: Search by position= parameter + position_id filter ===
                result = self._search_deal_by_position_param(position_ticket, open_price)
                if result:
                    return result
                
                # === LEVEL 2: Broad search ALL XAUUSD deals + position_id filter ===
                result = self._search_deal_broad(position_ticket, open_price)
                if result:
                    return result
                
                if attempt < total_attempts - 1:
                    log.warning(
                        f"Deal history: No close deal found for position_ticket={position_ticket} "
                        f"(attempt {attempt + 1}/{total_attempts})"
                    )
            
            # === LEVEL 3: P&L estimation from current price vs SL/TP ===
            log.warning(
                f"Deal history: No close deal found after {total_attempts} attempts for "
                f"position_ticket={position_ticket} — trying smart estimation"
            )
            return self._estimate_deal_from_tp_sl(position_ticket, open_price, tp_price, sl_price)
            
        except Exception as e:
            log.warning(f"Error querying deal history: {e}")
            return None
    
    def _search_deal_by_position_param(self, position_ticket: int, open_price: float = None) -> Optional[dict]:
        """Level 1: Search via position= parameter + position_id filter."""
        date_from = datetime.now() - timedelta(hours=48)
        date_to = datetime.now() + timedelta(hours=1)
        
        deals = mt5.history_deals_get(
            date_from,
            date_to,
            position=position_ticket
        )
        
        if deals is None or len(deals) == 0:
            log.debug(f"Deal history [N1]: No deals returned for position={position_ticket}")
            return None
        
        # Log all returned deals with ALL fields (full diagnostics)
        log.debug(f"Deal history [N1]: position={position_ticket} | {len(deals)} deals returned by MT5")
        for d in deals:
            match = "✓" if d.position_id == position_ticket else "✗"
            self._log_deal_full(d, match, "N1")
        
        # Filter by correct position_id
        correct_deals = [d for d in deals if d.position_id == position_ticket]
        wrong_count = len(deals) - len(correct_deals)
        if wrong_count > 0:
            log.warning(
                f"Deal history [N1]: position_id filter removed {wrong_count} of {len(deals)} deals "
                f"(wrong positions)"
            )
        
        if not correct_deals:
            log.debug(f"Deal history [N1]: No deals with position_id={position_ticket}")
            return None
        
        return self._extract_close_deal(correct_deals, position_ticket, open_price, "N1")
    
    def _search_deal_broad(self, position_ticket: int, open_price: float = None) -> Optional[dict]:
        """Level 2: Broad search ALL XAUUSD deals + position_id filter."""
        date_from = datetime.now() - timedelta(hours=48)
        date_to = datetime.now() + timedelta(hours=1)
        
        log.info(f"Deal history [N2]: Broad search — all XAUUSD deals in last 48h...")
        
        deals = mt5.history_deals_get(
            date_from,
            date_to,
            group=f"*{self.symbol}*"
        )
        
        if deals is None or len(deals) == 0:
            log.warning(f"Deal history [N2]: No XAUUSD deals found in last 48h")
            return None
        
        # Filter by position_id
        correct_deals = [d for d in deals if d.position_id == position_ticket]
        
        log.info(
            f"Deal history [N2]: {len(deals)} XAUUSD deals total, "
            f"{len(correct_deals)} with position_id={position_ticket}"
        )
        
        if not correct_deals:
            # Diagnostic log: show recent deals for debug
            recent_closes = [d for d in deals if d.entry != mt5.DEAL_ENTRY_IN]
            recent_closes.sort(key=lambda d: d.time, reverse=True)
            for d in recent_closes[:5]:
                self._log_deal_full(d, "?", "N2-recent")
            return None
        
        # Log found deals with all fields
        for d in correct_deals:
            self._log_deal_full(d, "✓", "N2")
        
        return self._extract_close_deal(correct_deals, position_ticket, open_price, "N2")
    
    def _extract_close_deal(self, deals: list, position_ticket: int, open_price: float, level: str) -> Optional[dict]:
        """Extract the close deal from a list of deals already filtered by position_id.
        
        Accepts DEAL_ENTRY_OUT, DEAL_ENTRY_INOUT (reverse) and DEAL_ENTRY_OUT_BY (close by opposite)
        as valid close deals. Only DEAL_ENTRY_IN (opening) is ignored.
        """
        for deal in deals:
            # Accept any entry that is not IN (opening)
            if deal.entry == mt5.DEAL_ENTRY_IN:
                continue
            
            # deal.entry is OUT, INOUT, or OUT_BY — all are valid closes
            entry_name = self._entry_type_name(deal.entry)
            
            # Determine close reason
            reason_map = {
                mt5.DEAL_REASON_SL: "Stop Loss",
                mt5.DEAL_REASON_TP: "Take Profit",
                mt5.DEAL_REASON_CLIENT: "Manual/Bot",
                mt5.DEAL_REASON_EXPERT: "Expert Advisor",
            }
            reason = reason_map.get(deal.reason, f"Other ({deal.reason})")
            
            # Original direction (inverse of close deal)
            direction = "BUY" if deal.type == mt5.DEAL_TYPE_SELL else "SELL"
            
            log.info(
                f"Deal history [{level}] FOUND: position_ticket={position_ticket} | "
                f"deal_ticket={deal.ticket} | entry={entry_name} | close_price={deal.price:.2f} | "
                f"profit={deal.profit:.2f} | reason={reason}"
            )
            
            return {
                'ticket': position_ticket,
                'deal_ticket': deal.ticket,
                'direction': direction,
                'volume': deal.volume,
                'close_price': deal.price,
                'profit': deal.profit,
                'commission': deal.commission,
                'swap': deal.swap,
                'reason': reason,
                'close_time': datetime.fromtimestamp(deal.time)
            }
        
        entry_types_found = [self._entry_type_name(d.entry) for d in deals]
        log.warning(
            f"Deal history [{level}]: {len(deals)} deals with correct position_id but no close deal "
            f"(entry types found: {entry_types_found})"
        )
        return None
    
    def _estimate_deal_from_tp_sl(self, position_ticket: int, open_price: float = None, tp_price: float = None, sl_price: float = None) -> Optional[dict]:
        """Level 3: Smart P&L estimation when no deal found in history.
        
        Uses the current MT5 tick price to determine if SL or TP was hit,
        based on proximity of current price to SL vs TP.
        """
        if open_price is None:
            log.warning(
                f"Deal history [N3]: Estimation impossible — open_price={open_price}"
            )
            return None
        
        if tp_price is None and sl_price is None:
            log.warning(
                f"Deal history [N3]: Estimation impossible — tp_price={tp_price}, sl_price={sl_price}"
            )
            return None
        
        # Determine direction: if TP > open → BUY, if TP < open → SELL
        if tp_price is not None:
            direction = "BUY" if tp_price > open_price else "SELL"
        elif sl_price is not None:
            direction = "BUY" if sl_price < open_price else "SELL"
        else:
            direction = "SELL"  # fallback
        
        # Try to get current MT5 tick price to decide SL vs TP
        estimated_close_price = None
        estimated_reason = None
        
        tick = mt5.symbol_info_tick(self.symbol)
        if tick is not None:
            current_price = tick.bid  # use bid as reference
            
            dist_to_sl = abs(current_price - sl_price) if sl_price else float('inf')
            dist_to_tp = abs(current_price - tp_price) if tp_price else float('inf')
            
            log.info(
                f"Deal history [N3]: Current price={current_price:.2f} | "
                f"SL={sl_price} (dist={dist_to_sl:.2f}) | TP={tp_price} (dist={dist_to_tp:.2f})"
            )
            
            if dist_to_sl <= dist_to_tp:
                estimated_close_price = sl_price
                estimated_reason = "Stop Loss (estimated)"
            else:
                estimated_close_price = tp_price
                estimated_reason = "Take Profit (estimated)"
        else:
            # No tick available — assume SL (conservative: assume loss)
            log.warning(f"Deal history [N3]: Tick unavailable — assuming SL hit (conservative)")
            if sl_price is not None:
                estimated_close_price = sl_price
                estimated_reason = "Stop Loss (estimated)"
            elif tp_price is not None:
                estimated_close_price = tp_price
                estimated_reason = "Take Profit (estimated)"
        
        if estimated_close_price is None:
            log.warning(f"Deal history [N3]: Could not estimate close price")
            return None
        
        # Determine outcome (WIN/LOSS/BE) from direction + close price vs open price
        pip_size = 0.1
        if direction == "BUY":
            pips = (estimated_close_price - open_price) / pip_size
        else:  # SELL
            pips = (open_price - estimated_close_price) / pip_size
        
        if pips > 0.5:
            outcome = "WIN"
        elif pips < -0.5:
            outcome = "LOSS"
        else:
            outcome = "BE"
        
        log.warning(
            f"Deal history [N3] PENDING: position_ticket={position_ticket} | "
            f"direction={direction} | open={open_price:.2f} → close≈{estimated_close_price:.2f} | "
            f"pips≈{pips:+.1f} | outcome={outcome} | "
            f"reason={estimated_reason} | "
            f"⚠️ P&L PENDING — real deal not yet in MT5 history, will resolve on next reconciliation"
        )
        
        return {
            'ticket': position_ticket,
            'deal_ticket': None,
            'direction': direction,
            'volume': 0.01,
            'close_price': estimated_close_price,
            'profit': None,
            'commission': 0,
            'swap': 0,
            'reason': estimated_reason,
            'close_time': datetime.now(),
            'estimated': True,
            'pending': True,
            'outcome': outcome,
        }
    
    def _entry_type_name(self, entry: int) -> str:
        """Return readable name for deal entry type."""
        entry_names = {
            mt5.DEAL_ENTRY_IN: "IN",
            mt5.DEAL_ENTRY_OUT: "OUT",
            mt5.DEAL_ENTRY_INOUT: "INOUT",
            mt5.DEAL_ENTRY_OUT_BY: "OUT_BY",
        }
        return entry_names.get(entry, f"UNKNOWN({entry})")
    
    def _log_deal_full(self, deal, match_symbol: str, level: str):
        """Log ALL fields of a deal for full diagnostics."""
        entry_name = self._entry_type_name(deal.entry)
        type_name = "BUY" if deal.type == mt5.DEAL_TYPE_BUY else ("SELL" if deal.type == mt5.DEAL_TYPE_SELL else f"OTHER({deal.type})")
        
        log.debug(
            f"  {match_symbol} [{level}] Deal #{deal.ticket} | pos_id={deal.position_id} | "
            f"entry={entry_name}(raw={deal.entry}) | type={type_name}(raw={deal.type}) | "
            f"price={deal.price:.2f} | profit={deal.profit:.2f} | volume={deal.volume} | "
            f"commission={deal.commission:.2f} | swap={deal.swap:.2f} | "
            f"reason={deal.reason} | time={datetime.fromtimestamp(deal.time)}"
        )
    
    def _get_error_message(self, code: int) -> str:
        """Return readable error message"""
        errors = {
            mt5.TRADE_RETCODE_REQUOTE: "Requote - price changed",
            mt5.TRADE_RETCODE_REJECT: "Order rejected by broker",
            mt5.TRADE_RETCODE_CANCEL: "Order cancelled",
            mt5.TRADE_RETCODE_PLACED: "Order placed",
            mt5.TRADE_RETCODE_DONE: "Order executed",
            mt5.TRADE_RETCODE_DONE_PARTIAL: "Order partially executed",
            mt5.TRADE_RETCODE_ERROR: "Generic error",
            mt5.TRADE_RETCODE_TIMEOUT: "Timeout",
            mt5.TRADE_RETCODE_INVALID: "Invalid request",
            mt5.TRADE_RETCODE_INVALID_VOLUME: "Invalid volume",
            mt5.TRADE_RETCODE_INVALID_PRICE: "Invalid price",
            mt5.TRADE_RETCODE_INVALID_STOPS: "Invalid SL/TP",
            mt5.TRADE_RETCODE_TRADE_DISABLED: "Trading disabled",
            mt5.TRADE_RETCODE_MARKET_CLOSED: "Market closed",
            mt5.TRADE_RETCODE_NO_MONEY: "Insufficient margin",
            mt5.TRADE_RETCODE_PRICE_CHANGED: "Price changed",
            mt5.TRADE_RETCODE_PRICE_OFF: "Price off market",
            mt5.TRADE_RETCODE_INVALID_EXPIRATION: "Invalid expiration",
            mt5.TRADE_RETCODE_ORDER_CHANGED: "Order modified",
            mt5.TRADE_RETCODE_TOO_MANY_REQUESTS: "Too many requests",
            mt5.TRADE_RETCODE_NO_CHANGES: "No changes",
            mt5.TRADE_RETCODE_SERVER_DISABLES_AT: "Server disabled autotrading",
            mt5.TRADE_RETCODE_CLIENT_DISABLES_AT: "Client disabled autotrading",
            mt5.TRADE_RETCODE_LOCKED: "Order locked",
            mt5.TRADE_RETCODE_FROZEN: "Order frozen",
            mt5.TRADE_RETCODE_INVALID_FILL: "Invalid fill type",
            mt5.TRADE_RETCODE_CONNECTION: "No server connection",
            mt5.TRADE_RETCODE_ONLY_REAL: "Real accounts only",
            mt5.TRADE_RETCODE_LIMIT_ORDERS: "Order limit reached",
            mt5.TRADE_RETCODE_LIMIT_VOLUME: "Volume limit reached",
            mt5.TRADE_RETCODE_INVALID_ORDER: "Invalid order",
            mt5.TRADE_RETCODE_POSITION_CLOSED: "Position already closed",
        }
        return errors.get(code, f"Unknown error ({code})")


# Global instance
executor = MT5Executor()


# ============================================================================
# CONVENIENCE FUNCTIONS
# ============================================================================

def connect_mt5() -> bool:
    """Connect to MT5"""
    return executor.connect()


def disconnect_mt5():
    """Disconnect from MT5"""
    executor.disconnect()


def is_mt5_connected() -> bool:
    """Check connection"""
    return executor.is_connected()


def get_account_balance() -> float:
    """Return account balance"""
    info = executor.get_account_info()
    return info['balance'] if info else 0


def execute_buy(
    lot_size: float,
    sl: float,
    tp: float,
    comment: str = "",
    confidence: Optional[float] = None,
    scenario: Optional[str] = None,
    risk_amount: Optional[float] = None,
    risk_percent: Optional[float] = None,
) -> OrderResult:
    """Execute buy order"""
    return executor.execute_trade(
        "BUY",
        lot_size,
        sl,
        tp,
        comment,
        confidence=confidence,
        scenario=scenario,
        risk_amount=risk_amount,
        risk_percent=risk_percent,
    )


def execute_sell(
    lot_size: float,
    sl: float,
    tp: float,
    comment: str = "",
    confidence: Optional[float] = None,
    scenario: Optional[str] = None,
    risk_amount: Optional[float] = None,
    risk_percent: Optional[float] = None,
) -> OrderResult:
    """Execute sell order"""
    return executor.execute_trade(
        "SELL",
        lot_size,
        sl,
        tp,
        comment,
        confidence=confidence,
        scenario=scenario,
        risk_amount=risk_amount,
        risk_percent=risk_percent,
    )


def get_positions() -> List[PositionInfo]:
    """Return open positions"""
    return executor.get_open_positions()


def close_position(ticket: int, volume: float = None) -> OrderResult:
    """Close position"""
    return executor.close_position(ticket, volume)


def modify_sl(ticket: int, new_sl: float) -> OrderResult:
    """Modify SL"""
    return executor.modify_position(ticket, new_sl=new_sl)


def modify_tp(ticket: int, new_tp: float) -> OrderResult:
    """Modify TP"""
    return executor.modify_position(ticket, new_tp=new_tp)


def get_deal_history(position_ticket: int, open_price: float = None, tp_price: float = None, sl_price: float = None) -> Optional[dict]:
    """Query deal history for a closed position"""
    return executor.get_deal_history(position_ticket, open_price=open_price, tp_price=tp_price, sl_price=sl_price)


def get_recent_closed_deals(hours: int = 48) -> List[dict]:
    """Return all XAUUSD close deals from the last N hours.
    
    Makes TWO MT5 API calls to work around a bug where long-range search
    silently omits recent deals from today:
      1) Long range (hours param) — catches history
      2) Today only (00:00 → tomorrow) — ensures today's deals
    Merge + dedup by deal.ticket.
    
    Returns:
        List of dicts with: position_id, deal_ticket, direction, volume,
        open_price, close_price, profit, commission, swap, reason,
        close_time, comment
    """
    if not executor.is_connected():
        return []
    
    try:
        now = datetime.now()
        symbol_filter = f"*{executor.symbol}*"
        
        # Call 1: long range (history)
        date_from_long = now - timedelta(hours=hours)
        date_to_long = now + timedelta(hours=1)
        deals_long = mt5.history_deals_get(date_from_long, date_to_long, group=symbol_filter)
        
        # Call 2: today only (works around MT5 long-range bug)
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        tomorrow_start = today_start + timedelta(days=1)
        deals_today = mt5.history_deals_get(today_start, tomorrow_start, group=symbol_filter)
        
        # Merge + dedup by deal.ticket
        seen_tickets = set()
        all_deals = []
        for deal_list in [deals_long, deals_today]:
            if deal_list is None:
                continue
            for deal in deal_list:
                if deal.ticket not in seen_tickets:
                    seen_tickets.add(deal.ticket)
                    all_deals.append(deal)
        
        log.info(
            f"get_recent_closed_deals: range={len(deals_long or [])} deals, "
            f"today={len(deals_today or [])} deals, merged={len(all_deals)} unique"
        )
        
        if not all_deals:
            return []
        
        # Index opening deals by position_id to get open_price
        open_deals = {}
        for deal in all_deals:
            if deal.entry == mt5.DEAL_ENTRY_IN:
                open_deals[deal.position_id] = deal
        
        result = []
        for deal in all_deals:
            # Ignore opening deals (IN)
            if deal.entry == mt5.DEAL_ENTRY_IN:
                continue
            
            # Determine close reason
            reason_map = {
                mt5.DEAL_REASON_SL: "Stop Loss",
                mt5.DEAL_REASON_TP: "Take Profit",
                mt5.DEAL_REASON_CLIENT: "Manual/Bot",
                mt5.DEAL_REASON_EXPERT: "Expert Advisor",
            }
            reason = reason_map.get(deal.reason, f"Other ({deal.reason})")
            
            # Original direction (inverse of close deal)
            direction = "BUY" if deal.type == mt5.DEAL_TYPE_SELL else "SELL"
            
            # Open price from corresponding opening deal
            open_deal = open_deals.get(deal.position_id)
            open_price = open_deal.price if open_deal else None
            
            result.append({
                'position_id': deal.position_id,
                'deal_ticket': deal.ticket,
                'direction': direction,
                'volume': deal.volume,
                'open_price': open_price,
                'close_price': deal.price,
                'profit': deal.profit,
                'commission': deal.commission,
                'swap': deal.swap,
                'reason': reason,
                'close_time': datetime.fromtimestamp(deal.time),
                'comment': getattr(open_deal, 'comment', '') if open_deal else '',
            })
        
        return result
    except Exception as e:
        log.warning(f"get_recent_closed_deals error: {e}")
        return []


# ============================================================================
# TEST
# ============================================================================

def test_executor():
    """Test the executor (DRY RUN mode)"""
    print("=" * 60)
    print("🧪 MT5 EXECUTOR TEST")
    print("=" * 60)
    
    # Force DRY RUN for test
    executor.dry_run = True
    
    print("\n📊 Test 1: Execute BUY (DRY RUN)")
    result = executor.execute_trade(
        direction="BUY",
        lot_size=0.02,
        stop_loss=2635.00,
        take_profit=2680.00,
        comment="Test-Buy"
    )
    print(f"   Success: {result.success}")
    print(f"   Ticket: {result.ticket}")
    
    print("\n📊 Test 2: Execute SELL (DRY RUN)")
    result = executor.execute_trade(
        direction="SELL",
        lot_size=0.01,
        stop_loss=2665.00,
        take_profit=2620.00,
        comment="Test-Sell"
    )
    print(f"   Success: {result.success}")
    print(f"   Ticket: {result.ticket}")
    
    print("\n📊 Test 3: Close position (DRY RUN)")
    result = executor.close_position(ticket=999999, volume=0.01)
    print(f"   Success: {result.success}")
    
    print("\n📊 Test 4: Modify SL (DRY RUN)")
    result = executor.modify_position(ticket=999999, new_sl=2640.00)
    print(f"   Success: {result.success}")
    
    print("\n✅ Tests complete!")


if __name__ == "__main__":
    test_executor()
