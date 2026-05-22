"""
ORDER EXECUTOR - MT5 Order Execution
Automatically sends orders to MetaTrader 5
"""

# FLO-348: thread-safe MT5 proxy; every mt5.* call auto-locks via mt5_safe.mt5_lock
from mt5_safe import mt5, mt5_lock
import os  # FLO-291 fix: os.path.exists at L416 NameError'd every signal write,
           # leaving the signal-ID duplicate-prevention gate silently inert.
import threading
import time
from dataclasses import dataclass
from typing import Optional, Tuple, List
from datetime import datetime, timedelta
from tz_utils import utc_now  # FLO-309: local→UTC cleanup
import config

# FLO-348: module-level lock serialising the three write methods
# (execute_trade, modify_position, close_position) across concurrent
# callers (Floki main loop, monitor.py, Snow FLO-347). RLock so
# re-entry from the same thread is safe (e.g. retry logic inside
# execute_trade that calls close_position on phantom cleanup).
executor_lock: threading.RLock = threading.RLock()


def _with_executor_lock(fn):
    """FLO-348 decorator: serialise execute_trade/modify_position/close_position.

    RLock is acquired for the full method lifetime so concurrent callers
    (Snow, Floki, monitor.py) observe atomic end-to-end state transitions,
    not just atomic per-mt5-call (which mt5_lock already covers).
    """
    def wrapper(self, *args, **kwargs):
        with executor_lock:
            return fn(self, *args, **kwargs)
    wrapper.__name__ = fn.__name__
    wrapper.__doc__ = fn.__doc__
    wrapper.__wrapped__ = fn
    return wrapper

# FLO-96: MT5 server time offset (shared helper, same as main.py)
_mt5_offset_cache_ex = {"value": 10800, "computed_at": 0.0}

def _mt5_server_offset() -> int:
    """Return seconds to subtract from MT5 timestamps to get true UTC epoch."""
    if time.time() - _mt5_offset_cache_ex["computed_at"] < 3600:
        return _mt5_offset_cache_ex["value"]
    try:
        _tick = mt5.symbol_info_tick("XAUUSD")
        if _tick and _tick.time > 0:
            offset = int(_tick.time) - int(time.time())
            _mt5_offset_cache_ex.update({"value": offset, "computed_at": time.time()})
            return offset
    except Exception:
        pass
    return _mt5_offset_cache_ex["value"]
from logger import log
from alerts import (
    alert_trade_executed, alert_trade_closed, 
    alert_error, alert_trailing_stop
)
from floki_position_manager import get_ea_management_params


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
        # Bug D: timestamp of last connect() attempt (any caller: self-recovery
        # path or monitor.py:917 DEAL_REFRESH). Zero = never attempted.
        self._last_reconnect_attempt = 0.0

    def connect(self) -> bool:
        """Connect to MT5"""
        # Bug D: stamp every connect() attempt so the cooldown in
        # _try_reconnect_once() reflects ALL callers (self-recovery AND
        # monitor.py:917 DEAL_REFRESH path). Both paths share the same
        # cooldown window via this timestamp.
        self._last_reconnect_attempt = time.time()
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
        """Fast connection-flag check (no MT5 probe).

        Bug D: was previously a live probe that latched the flag to False
        on any transient None from mt5.account_info(), with no recovery
        path — one stall would permanently disconnect the executor instance
        until the bot restarted. Now flag-only; the flag is maintained by
        get_account_info() which owns the retry + reconnect recovery logic.
        For fresh live verification call get_account_info() instead.
        """
        return bool(self.connected)

    def _read_account_info_with_retry(self):
        """Read mt5.account_info() with retries on transient None.

        Bug D: MT5 Python API is known to return None transiently during
        terminal resync / GUI-busy / broker server stalls (typically
        <500ms). Retry 3x with exponential backoff (100ms, 300ms, 900ms;
        total budget ~1.3s) before declaring the read failed.

        Returns native mt5 AccountInfo object on success, None if all
        retries fail.
        """
        account = mt5.account_info()
        if account is not None:
            return account
        for i, delay_ms in enumerate((100, 300, 900), 1):
            log.warning(f"MT5 | STALE_READ | account_info None, retry {i}/3 after {delay_ms}ms")
            time.sleep(delay_ms / 1000.0)
            account = mt5.account_info()
            if account is not None:
                log.info(f"MT5 | STALE_READ | resolved after retry {i}")
                return account
        return None

    def _try_reconnect_once(self, cooldown_s: float = 60.0) -> bool:
        """Attempt single reconnect via self.connect(), gated by cooldown.

        60s cooldown prevents reconnect storm when MT5 is genuinely offline.
        Chosen as balance between responsiveness (try again within a minute)
        and restraint (don't hammer mt5.initialize() on every caller).

        Returns True if connect() was attempted and succeeded; False if
        skipped due to cooldown or if the attempt failed.
        """
        now = time.time()
        elapsed = now - self._last_reconnect_attempt
        if elapsed < cooldown_s:
            remaining = int(cooldown_s - elapsed)
            log.info(f"MT5 | RECONNECT | skipped (cooldown, {remaining}s remaining)")
            return False
        log.warning(f"MT5 | RECONNECT | attempt (last {int(elapsed)}s ago)")
        try:
            ok = self.connect()
            if ok:
                log.info("MT5 | RECONNECT | success")
                return True
            log.error(f"MT5 | RECONNECT | failed: {mt5.last_error()}")
            return False
        except Exception as e:
            log.error(f"MT5 | RECONNECT | failed: {e}")
            return False

    def get_account_info(self) -> Optional[dict]:
        """Return account information with retry + auto-reconnect recovery.

        Bug D: previously a thin wrapper around mt5.account_info() that
        returned None on any transient stall, silently latching is_connected
        to False. Now owns the recovery flow:
          1. Read with retry (handles transient None: ~1.3s budget)
          2. If retries exhausted, attempt single reconnect (cooldown-gated)
          3. If still unavailable, flip self.connected=False with visible log

        Happy path: 1 MT5 call, zero retries, zero logs, zero sleep.
        """
        if not self.connected:
            return None

        account = self._read_account_info_with_retry()
        if account is None:
            # Retries exhausted — attempt one reconnect (respects cooldown)
            if self._try_reconnect_once():
                account = mt5.account_info()
                if account is not None:
                    log.mt5_status(True, "recovered after reconnect")
            if account is None:
                # Genuinely unavailable: flip flag with visible log
                if self.connected:
                    self.connected = False
                    log.mt5_status(False, "latched after stale-read + reconnect failure")
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
    
    @_with_executor_lock  # FLO-348
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

        # EA Bridge execution path (preferred when online)
        if getattr(config, "USE_EA_BRIDGE", False):
            try:
                from ea_bridge import is_ea_online, write_signal

                stale_threshold = getattr(config, "EA_STALE_THRESHOLD_SECONDS", 60)
                if is_ea_online(stale_threshold):
                    # Determine reference price for SL-distance calculation
                    ref_price = ask if direction.upper() == "BUY" else bid
                    sl_pips = abs(ref_price - float(stop_loss)) / 0.1

                    be_pips, tr_trig_pips, tr_dist_pips, max_drawdown_pips = get_ea_management_params(
                        sl_pips,
                        None,
                    )

                    # FLO-89: Snapshot positions BEFORE write_signal to avoid race condition
                    pre_tickets = set()
                    try:
                        from ea_bridge import read_ea_status
                        pre_status = read_ea_status(stale_threshold_seconds=120)
                        if pre_status and pre_status.positions:
                            pre_tickets = {p.ticket for p in pre_status.positions}
                    except Exception as e_pre:
                        log.warning(f"EA_BRIDGE | Pre-snapshot failed (non-blocking): {e_pre}")

                    # FLO-282: ALSO snapshot MT5 directly — EA status JSON can be stale
                    # exactly when the EA is slow. Querying MT5 is the source of truth
                    # for "which positions existed BEFORE we fired this signal".
                    mt5_pre_tickets = set()
                    try:
                        _mt5_pre = mt5.positions_get(symbol=self.symbol)
                        if _mt5_pre:
                            mt5_pre_tickets = {p.ticket for p in _mt5_pre if p.magic == self.magic}
                    except Exception as e_mt5_pre:
                        log.warning(f"EA_BRIDGE | MT5 pre-snapshot failed (non-blocking): {e_mt5_pre}")

                    ok = write_signal(
                        signal=direction,
                        sl=float(stop_loss),
                        tp=float(take_profit),
                        lot_size=float(lot_size),
                        confidence=float(confidence) if confidence is not None else 50.0,
                        breakeven_trigger_pips=be_pips,
                        trailing_trigger_pips=tr_trig_pips,
                        trailing_distance_pips=tr_dist_pips,
                        max_drawdown_pips=max_drawdown_pips,
                        comment=comment,
                    )

                    if ok:
                        # FLO-291: Capture our signal_id for the pre-fallthrough gate below.
                        # Read back the signal file we just wrote — EA hasn't consumed it yet
                        # (~1s polling gap minimum). Used later to match against EA's
                        # last_signal_id to detect late EA fills before direct OrderSend.
                        current_signal_id = None
                        try:
                            from ea_bridge import get_signal_file_path
                            _sig_path = get_signal_file_path()
                            if os.path.exists(_sig_path):
                                with open(_sig_path, 'r', encoding='utf-8') as _sf:
                                    import json as _json_sig
                                    current_signal_id = _json_sig.load(_sf).get('signal_id')
                        except Exception as e_sig_cap:
                            log.debug(f"EA_BRIDGE | FLO-291 signal_id capture failed (non-blocking): {e_sig_cap}")

                        # Poll EA status for real ticket instead of returning 0.
                        # The EA processes signals within seconds; poll up to 10s.
                        real_ticket = 0
                        try:
                            from ea_bridge import read_ea_status
                            import time as _time

                            for _poll in range(10):
                                _time.sleep(1)
                                post_status = read_ea_status(stale_threshold_seconds=120)
                                if not post_status:
                                    continue
                                # Look for a new position not in the pre-snapshot
                                for p in post_status.positions:
                                    if p.ticket not in pre_tickets and p.ticket > 0:
                                        if p.direction.upper() == direction.upper():
                                            real_ticket = p.ticket
                                            log.info(f"EA_BRIDGE | Real ticket resolved: {real_ticket} (poll {_poll + 1}s)")
                                            break
                                if real_ticket > 0:
                                    break
                            # FLO-197 + FLO-282: Last-resort phantom check via MT5 direct API.
                            # Use mt5_pre_tickets (MT5 ground truth) instead of pre_tickets
                            # (which comes from EA status JSON — can be stale exactly when EA is slow).
                            if real_ticket == 0:
                                try:
                                    _final_pos = mt5.positions_get(symbol=self.symbol)
                                    if _final_pos:
                                        for p in _final_pos:
                                            if p.magic == self.magic and p.ticket not in mt5_pre_tickets:
                                                _dir_match = (
                                                    (p.type == mt5.POSITION_TYPE_BUY and direction.upper() == "BUY")
                                                    or (p.type == mt5.POSITION_TYPE_SELL and direction.upper() == "SELL")
                                                )
                                                if _dir_match:
                                                    real_ticket = p.ticket
                                                    log.warning(
                                                        f"PHANTOM_POSITION | EA poll failed but MT5 has "
                                                        f"ticket #{p.ticket} — recovering"
                                                    )
                                                    # FLO-98: Alert Hermano about phantom recovery
                                                    alert_error(
                                                        "Phantom Position Recovered",
                                                        f"EA poll failed but MT5 opened ticket #{p.ticket} ({direction}). Position recovered and being managed.",
                                                        severity="warning",
                                                    )
                                                    break
                                except Exception as e_phantom:
                                    log.warning(f"PHANTOM_POSITION | EA path detection failed: {e_phantom}")

                            if real_ticket == 0:
                                log.warning("EA_BRIDGE | Could not resolve real ticket after 10s — falling through to MT5 direct API")
                                # FLO-263: Clear the signal file BEFORE falling through to prevent
                                # the EA from processing the stale signal (phantom double-execution risk).
                                try:
                                    write_signal(signal="HOLD", sl=0, tp=0, lot_size=0, confidence=0,
                                                 breakeven_trigger_pips=0, trailing_trigger_pips=0,
                                                 trailing_distance_pips=0, max_drawdown_pips=0, comment="fallthrough_clear")
                                    log.info("EA_BRIDGE | Signal cleared (HOLD) before MT5 direct fallthrough")
                                except Exception:
                                    pass

                                # FLO-282: FINAL safety check — between signal clear and direct
                                # submission, the EA's order may have completed on the broker.
                                # Query MT5 ONE MORE TIME against mt5_pre_tickets (the original
                                # ground-truth snapshot from before write_signal).
                                try:
                                    _ultra_final = mt5.positions_get(symbol=self.symbol)
                                    if _ultra_final:
                                        for p in _ultra_final:
                                            if p.magic == self.magic and p.ticket not in mt5_pre_tickets:
                                                _ud_match = (
                                                    (p.type == mt5.POSITION_TYPE_BUY and direction.upper() == "BUY")
                                                    or (p.type == mt5.POSITION_TYPE_SELL and direction.upper() == "SELL")
                                                )
                                                if _ud_match:
                                                    real_ticket = p.ticket
                                                    log.warning(
                                                        f"EA_BRIDGE | EA filled during polling window "
                                                        f"(ticket #{p.ticket}) — skipping direct fallthrough "
                                                        f"to prevent duplicate position"
                                                    )
                                                    alert_error(
                                                        "Duplicate Order Prevented",
                                                        f"EA filled #{p.ticket} ({direction}) while Python was "
                                                        f"polling. Direct MT5 submission was about to fire — "
                                                        f"caught and skipped.",
                                                        severity="warning",
                                                    )
                                                    break
                                except Exception as e_final:
                                    log.warning(f"EA_BRIDGE | Final pre-fallthrough MT5 check failed: {e_final}")

                                # FLO-291: Signal-ID gate — final defense against duplicate execution.
                                # If the EA has ack'd our signal_id as success but MT5 hasn't surfaced
                                # the position yet, fallthrough would create a second order. Check the
                                # EA's last_signal_id against the signal_id we captured post-write,
                                # and if it matches with a success result, poll MT5 for up to 2s.
                                if real_ticket == 0 and current_signal_id:
                                    try:
                                        _ack = read_ea_status(stale_threshold_seconds=5)
                                        if _ack and _ack.last_signal_id == current_signal_id:
                                            _res_lc = (_ack.last_signal_result or "").lower()
                                            _ea_success = any(k in _res_lc for k in (
                                                "ok", "ticket", "opened", "success", "filled"
                                            ))
                                            if _ea_success:
                                                log.warning(
                                                    f"EA_BRIDGE | FLO-291 gate: EA ack'd signal_id "
                                                    f"{current_signal_id} as success "
                                                    f"({_ack.last_signal_result!r}) — polling MT5 2s "
                                                    f"for late fill before fallthrough"
                                                )
                                                for _wait in range(10):
                                                    _time.sleep(0.2)
                                                    _late = mt5.positions_get(symbol=self.symbol)
                                                    if _late:
                                                        for p in _late:
                                                            if p.magic == self.magic and p.ticket not in mt5_pre_tickets:
                                                                _dm = (
                                                                    (p.type == mt5.POSITION_TYPE_BUY and direction.upper() == "BUY")
                                                                    or (p.type == mt5.POSITION_TYPE_SELL and direction.upper() == "SELL")
                                                                )
                                                                if _dm:
                                                                    real_ticket = p.ticket
                                                                    log.warning(
                                                                        f"EA_BRIDGE | FLO-291 gate caught late EA fill "
                                                                        f"(signal_id={current_signal_id}, ticket=#{p.ticket}, "
                                                                        f"+{(_wait + 1) * 200}ms wait) — skipping direct API"
                                                                    )
                                                                    alert_error(
                                                                        "Duplicate Order Prevented (FLO-291)",
                                                                        f"EA ack'd signal_id {current_signal_id} as success. "
                                                                        f"Fallthrough was about to fire. Ticket #{p.ticket} found "
                                                                        f"after {(_wait + 1) * 200}ms extra wait — fallthrough cancelled.",
                                                                        severity="warning",
                                                                    )
                                                                    break
                                                        if real_ticket > 0:
                                                            break
                                    except Exception as e_sig_gate:
                                        log.warning(f"EA_BRIDGE | FLO-291 signal_id gate error (non-blocking): {e_sig_gate}")
                        except Exception as e_poll:
                            log.warning(f"EA_BRIDGE | Ticket poll error (non-blocking): {e_poll}")
                            # FLO-197: Re-run phantom check after exception — position
                            # may have been opened despite the polling error.
                            if real_ticket == 0:
                                try:
                                    _rescue_pos = mt5.positions_get(symbol=self.symbol)
                                    if _rescue_pos:
                                        for p in _rescue_pos:
                                            if p.magic == self.magic and p.ticket not in pre_tickets:
                                                _dir_ok = (
                                                    (p.type == mt5.POSITION_TYPE_BUY and direction.upper() == "BUY")
                                                    or (p.type == mt5.POSITION_TYPE_SELL and direction.upper() == "SELL")
                                                )
                                                if _dir_ok:
                                                    real_ticket = p.ticket
                                                    log.warning(
                                                        f"PHANTOM_POSITION | Post-exception rescue found "
                                                        f"ticket #{p.ticket} — recovering"
                                                    )
                                                    alert_error(
                                                        "Phantom Position Recovered (post-exception)",
                                                        f"Polling failed with {e_poll}, but MT5 has ticket #{p.ticket} ({direction}). Recovered.",
                                                        severity="warning",
                                                    )
                                                    break
                                except Exception as e_rescue:
                                    log.warning(f"PHANTOM_POSITION | Post-exception rescue failed: {e_rescue}")

                        if real_ticket > 0:
                            return OrderResult(
                                success=True,
                                ticket=real_ticket,
                                error_code=None,
                                error_message=None,
                                price=ref_price,
                                volume=lot_size,
                            )
                        # real_ticket == 0: fall through to direct MT5 API below
                        log.info("EA_BRIDGE | ticket_not_resolved after polling — retrying via MT5 direct API")
            except Exception as e_ea:
                log.error(f"EA_BRIDGE | Failed, falling through to MT5 direct: {e_ea}")
        
        # FLO-197: Pre-snapshot for phantom position detection (MT5 direct path)
        pre_tickets_direct = set()
        try:
            _pre_pos = mt5.positions_get(symbol=self.symbol)
            if _pre_pos:
                pre_tickets_direct = {p.ticket for p in _pre_pos if p.magic == self.magic}
        except Exception:
            pass

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

            # FLO-197: Phantom position detection — MT5 may have opened despite error
            if result.retcode in (mt5.TRADE_RETCODE_TIMEOUT, 10004):  # TIMEOUT or REQUOTE
                try:
                    import time as _time
                    _time.sleep(2)  # Brief wait for MT5 to settle
                    _post_pos = mt5.positions_get(symbol=self.symbol)
                    if _post_pos:
                        for p in _post_pos:
                            if p.magic == self.magic and p.ticket not in pre_tickets_direct:
                                _dir_match = (
                                    (p.type == mt5.POSITION_TYPE_BUY and direction.upper() == "BUY")
                                    or (p.type == mt5.POSITION_TYPE_SELL and direction.upper() == "SELL")
                                )
                                if _dir_match:
                                    log.warning(
                                        f"PHANTOM_POSITION | execute_trade returned {result.retcode} "
                                        f"but MT5 opened ticket #{p.ticket} — recovering"
                                    )
                                    # FLO-98: Alert Hermano about phantom recovery
                                    alert_error(
                                        "Phantom Position Recovered",
                                        f"MT5 returned error {result.retcode} but opened ticket #{p.ticket} ({direction}). Position recovered.",
                                        severity="warning",
                                    )
                                    return OrderResult(
                                        success=True,
                                        ticket=p.ticket,
                                        error_code=None,
                                        error_message=None,
                                        price=p.price_open,
                                        volume=p.volume,
                                    )
                except Exception as e_phantom:
                    log.warning(f"PHANTOM_POSITION | Detection check failed: {e_phantom}")

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

        # FLO-338 B: post-fill duplicate scan (MT5-direct success path only; EA path
        # already has FLO-282 + FLO-291). Defense-in-depth for the rare race where the
        # EA places an order AFTER direct fallback completes. Phase 1.5 data showed
        # 0/20 known ghosts had this signature, but this is cheap insurance.
        try:
            import config as _cfg_b
            _b_on = bool(getattr(_cfg_b, "GHOST_GUARDS_ENABLED", True))
        except Exception:
            _b_on = True
        if _b_on:
            try:
                import time as _tb
                _tb.sleep(1.5)  # settle window for late EA arrival
                _post = mt5.positions_get(symbol=self.symbol)
                if _post:
                    for _p in _post:
                        if (_p.magic == self.magic and _p.ticket != result.order
                                and _p.ticket not in pre_tickets_direct):
                            _dm = ((_p.type == mt5.POSITION_TYPE_BUY and direction.upper() == "BUY")
                                   or (_p.type == mt5.POSITION_TYPE_SELL and direction.upper() == "SELL"))
                            if _dm:
                                log.warning(
                                    f"GHOST_GUARD_B | duplicate detected ticket=#{_p.ticket} "
                                    f"(kept=#{result.order}); closing duplicate"
                                )
                                _close_req = {
                                    "action": mt5.TRADE_ACTION_DEAL,
                                    "symbol": self.symbol,
                                    "volume": _p.volume,
                                    "type": mt5.ORDER_TYPE_SELL if _p.type == mt5.POSITION_TYPE_BUY else mt5.ORDER_TYPE_BUY,
                                    "position": _p.ticket,
                                    "deviation": config.MAX_SLIPPAGE_PIPS,
                                    "magic": self.magic,
                                    "comment": "ghost_guard_b_dup_close",
                                    "type_filling": mt5.ORDER_FILLING_IOC,
                                }
                                _cr = mt5.order_send(_close_req)
                                _ok = bool(_cr and getattr(_cr, "retcode", 0) == mt5.TRADE_RETCODE_DONE)
                                alert_error(
                                    "Ghost Guard B: Duplicate Closed" if _ok else "Ghost Guard B: Close FAILED",
                                    f"Duplicate ticket #{_p.ticket} ({direction}) detected after direct "
                                    f"fallback; close={'OK' if _ok else 'FAILED retcode=' + str(getattr(_cr, 'retcode', '?'))}. "
                                    f"Kept ticket #{result.order}.",
                                    severity="warning",
                                )
            except Exception as e_b:
                log.warning(f"GHOST_GUARD_B | scan failed (non-blocking): {e_b}")

        return OrderResult(
            success=True,
            ticket=result.order,
            error_code=None,
            error_message=None,
            price=result.price,
            volume=result.volume
        )

    # ================================================================
    # PENDING ORDERS (FLO-263)
    # ================================================================

    def place_pending_order(self, order_type_str: str, price: float, lot_size: float,
                            stop_loss: float, take_profit: float,
                            expiry_minutes: int = 0, comment: str = "") -> dict:
        """Place a pending order (BUY_LIMIT/SELL_LIMIT/BUY_STOP/SELL_STOP) via MT5."""
        # FLO-348: inline import removed; module-level `mt5` is the thread-safe proxy

        _TYPE_MAP = {
            "BUY_LIMIT": mt5.ORDER_TYPE_BUY_LIMIT,
            "SELL_LIMIT": mt5.ORDER_TYPE_SELL_LIMIT,
            "BUY_STOP": mt5.ORDER_TYPE_BUY_STOP,
            "SELL_STOP": mt5.ORDER_TYPE_SELL_STOP,
        }
        mt5_type = _TYPE_MAP.get(order_type_str.upper())
        if mt5_type is None:
            return {"success": False, "error": f"Invalid order type: {order_type_str}"}

        request = {
            "action": mt5.TRADE_ACTION_PENDING,
            "symbol": self.symbol,
            "volume": lot_size,
            "type": mt5_type,
            "price": price,
            "sl": stop_loss,
            "tp": take_profit,
            "magic": self.magic,
            "comment": comment or f"Pending-{order_type_str}",
            "type_filling": mt5.ORDER_FILLING_IOC,
        }
        if expiry_minutes and expiry_minutes > 0:
            # Expiration must be in broker server time (UTC+3), not Python local time (FLO-96)
            _tick = mt5.symbol_info_tick(self.symbol)
            _server_now = int(_tick.time) if _tick else 0
            if _server_now > 0:
                request["type_time"] = mt5.ORDER_TIME_SPECIFIED
                request["expiration"] = _server_now + (int(expiry_minutes) * 60)
            else:
                request["type_time"] = mt5.ORDER_TIME_GTC  # fallback: no expiry
        else:
            request["type_time"] = mt5.ORDER_TIME_GTC

        check = mt5.order_check(request)
        if check is None or check.retcode != 0:
            _comment = getattr(check, "comment", "unknown") if check else "order_check returned None"
            log.warning(f"PENDING_ORDER | order_check failed: {_comment}")
            return {"success": False, "error": f"order_check failed: {_comment}"}

        result = mt5.order_send(request)
        if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
            _rc = getattr(result, "retcode", "?") if result else "None"
            _cm = getattr(result, "comment", "") if result else ""
            log.warning(f"PENDING_ORDER | order_send failed: retcode={_rc} {_cm}")
            return {"success": False, "error": f"order_send failed: retcode={_rc} {_cm}"}

        log.info(f"PENDING_ORDER | PLACED {order_type_str} @ {price} | SL={stop_loss} TP={take_profit} | "
                 f"lot={lot_size} | expiry={expiry_minutes}min | ticket={result.order}")
        return {"success": True, "ticket": result.order, "type": order_type_str, "price": price}

    def cancel_pending_order(self, ticket: int) -> dict:
        """Cancel a pending order by ticket."""
        # FLO-348: inline import removed; module-level `mt5` is the thread-safe proxy

        request = {"action": mt5.TRADE_ACTION_REMOVE, "order": ticket}
        result = mt5.order_send(request)
        if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
            _rc = getattr(result, "retcode", "?") if result else "None"
            log.warning(f"PENDING_ORDER | cancel failed: ticket={ticket} retcode={_rc}")
            return {"success": False, "error": f"cancel failed: retcode={_rc}"}

        log.info(f"PENDING_ORDER | CANCELLED ticket={ticket}")
        return {"success": True, "ticket": ticket}

    def cancel_all_pending(self) -> dict:
        """Cancel all pending orders for this symbol with our magic number."""
        # FLO-348: inline import removed; module-level `mt5` is the thread-safe proxy

        orders = mt5.orders_get(symbol=self.symbol)
        cancelled = 0
        if orders:
            for o in orders:
                if o.magic == self.magic:
                    self.cancel_pending_order(o.ticket)
                    cancelled += 1
        if cancelled:
            log.info(f"PENDING_ORDER | CANCEL_ALL | cancelled={cancelled}")
        return {"success": True, "cancelled": cancelled}

    def get_pending_orders(self) -> list:
        """List all pending orders for this symbol with our magic number."""
        # FLO-348: inline import removed; module-level `mt5` is the thread-safe proxy

        orders = mt5.orders_get(symbol=self.symbol)
        result = []
        if orders:
            _type_names = {2: "BUY_LIMIT", 3: "SELL_LIMIT", 4: "BUY_STOP", 5: "SELL_STOP"}
            for o in orders:
                if o.magic == self.magic:
                    result.append({
                        "ticket": o.ticket,
                        "type": _type_names.get(o.type, str(o.type)),
                        "price": o.price_open,
                        "sl": o.sl,
                        "tp": o.tp,
                        "volume": o.volume_initial,
                        "time_setup": str(o.time_setup),
                    })
        return result

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
                open_time=datetime.utcfromtimestamp(int(pos.time) - _mt5_server_offset()),
                magic=pos.magic,
                comment=pos.comment
            ))
        
        return result
    
    @_with_executor_lock  # FLO-348
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
    
    @_with_executor_lock  # FLO-348
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

        # FLO-419 universal monotonic SL guard. The trail_sl-monotonic fix
        # in snow/actions.py only covers Snow's trail action. Any other
        # writer — Snow's adjust_sl/move_sl_to_price, Qwen's adjust_trade,
        # Monitor's _check_trailing_stop / _check_breakeven, or a future
        # caller — could still loosen SL. This is the single bottleneck;
        # enforce the invariant here so it holds regardless of caller.
        # BUY: SL can only move UP (toward price). SELL: SL can only move
        # DOWN (toward price). First SL set (current == 0) bypasses.
        # Loosen attempts are rejected with a WARNING, not silently clamped,
        # so the caller's wrong intent is visible.
        if new_sl is not None:
            current_sl = float(position.sl or 0.0)
            if current_sl > 0.0:
                is_buy = position.type == mt5.POSITION_TYPE_BUY
                loosens = (
                    (is_buy and new_sl < current_sl) or
                    (not is_buy and new_sl > current_sl)
                )
                if loosens:
                    direction = "BUY" if is_buy else "SELL"
                    msg = (
                        f"SL_GUARD ticket={ticket} {direction} rejected: "
                        f"new_sl={new_sl:.5f} would loosen current_sl={current_sl:.5f}"
                    )
                    log.warning(msg)
                    return OrderResult(
                        success=False,
                        ticket=None,
                        error_code=-5,
                        error_message=msg,
                        price=None,
                        volume=None,
                    )

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
                
                # === LEVEL 2.5: Today-only search (MT5 long-range omission workaround) ===
                result = self._search_deal_today_only(position_ticket, open_price)
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

    def _search_deal_today_only(self, position_ticket: int, open_price: float = None) -> Optional[dict]:
        """Level 2.5: Today-only search to catch recent deals omitted by long-range MT5 queries.

        FLO-292: MT5 history_deals_get treats naive datetime args as broker-local
        time. With local CEST (UTC+2) and broker EEST (UTC+3) — the gap that
        existed when this code was written — naive `datetime.now()` produced a
        window 1h SHORT on the upper bound, missing trades that closed in the
        last broker hour of the day (e.g. #1589450832 closed at broker 00:50
        next day / UTC 21:50 — outside a window that ended at broker 23:00).

        Fix: extend the upper bound by the live broker offset + a generous
        safety margin. Cost is just more rows to position_id-filter on; never
        misses a deal that exists in MT5 history.
        """
        import time as _t
        try:
            tick = mt5.symbol_info_tick(self.symbol)
            offset_s = int(tick.time) - int(_t.time()) if tick and tick.time else 0
        except Exception:
            offset_s = 0

        now_local = datetime.now()
        today_start = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
        # Extend upper bound: tomorrow + broker offset + 1h headroom for DST shifts.
        end = today_start + timedelta(days=1, seconds=max(offset_s, 0) + 3600)

        log.info(
            f"Deal history [N2.5]: window {today_start} -> {end} "
            f"(broker_offset={offset_s/3600:.1f}h)"
        )

        deals = mt5.history_deals_get(
            today_start,
            end,
            group=f"*{self.symbol}*",
        )

        if deals is None or len(deals) == 0:
            log.warning(f"Deal history [N2.5]: No XAUUSD deals found today")
            return None

        correct_deals = [d for d in deals if d.position_id == position_ticket]
        log.info(
            f"Deal history [N2.5]: {len(deals)} XAUUSD deals total (today), "
            f"{len(correct_deals)} with position_id={position_ticket}"
        )

        if not correct_deals:
            recent_closes = [d for d in deals if d.entry != mt5.DEAL_ENTRY_IN]
            recent_closes.sort(key=lambda d: d.time, reverse=True)
            for d in recent_closes[:5]:
                self._log_deal_full(d, "?", "N2.5-recent")
            return None

        for d in correct_deals:
            self._log_deal_full(d, "✓", "N2.5")

        return self._extract_close_deal(correct_deals, position_ticket, open_price, "N2.5")
    
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
                'close_time': datetime.utcfromtimestamp(int(deal.time) - _mt5_server_offset())
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
            # FLO-309: was datetime.now() — main.py:489 passes .isoformat() to
            # record_trade_close, so local time would land in history.db
            # close_time stamped as UTC. Use aware UTC instead.
            'close_time': utc_now(),
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
            f"reason={deal.reason} | time={datetime.utcfromtimestamp(int(deal.time) - _mt5_server_offset())}"
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
        
        # Call 2: today only (works around MT5 long-range bug).
        # FLO-292: extend upper bound by broker offset + 1h headroom so deals
        # that close in the last broker hour (when broker > local timezone)
        # aren't missed. Naive datetimes are interpreted as broker-local by MT5.
        import time as _t
        try:
            tick = mt5.symbol_info_tick(executor.symbol)
            offset_s = int(tick.time) - int(_t.time()) if tick and tick.time else 0
        except Exception:
            offset_s = 0
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        tomorrow_start = today_start + timedelta(days=1, seconds=max(offset_s, 0) + 3600)
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
                'open_time': datetime.utcfromtimestamp(int(open_deal.time) - _mt5_server_offset()) if open_deal else None,
                'close_time': datetime.utcfromtimestamp(int(deal.time) - _mt5_server_offset()),
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
