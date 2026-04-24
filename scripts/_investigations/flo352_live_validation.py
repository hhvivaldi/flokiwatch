"""FLO-352 live validation — exercise executor.modify_position end-to-end on real demo.

Purpose: deterministic in-production-code-path check that the FLO-352 fix
(commit 8d1fd2c) resolved the modify_position regression. Runs the SAME
code path Floki uses at runtime (agent_tools.adjust_trade →
executor.modify_position, decorated with @_with_executor_lock, going
through the mt5_safe proxy).

⚠️  REQUIRES BOT STOPPED. ⚠️

WHAT IT DOES

  1. Refuse to run if bot.pid points at a live process.
  2. Refuse to run if demo account has ANY open position
     (whitelisted stale FLO352_* test positions can be auto-cleaned with
     --auto-clean-stale; others abort with explicit instructions).
  3. Initialize MT5 + executor.connect().
  4. Open one controlled BUY 0.01 lot with:
       SL = entry - 15.0 (150 pt below)
       TP = entry + 15.0 (150 pt above)
       comment = "FLO352_LIVE_VALIDATION"
     Entry price is live ask at open time.
  5. Perform THREE executor.modify_position calls, each widening TP by
     1 price unit (10 pips). SL is held CONSTANT to avoid any interaction
     with potential SL guards and to keep the position safe:
       Mod 1: SL = entry - 15, TP = entry + 16
       Mod 2: SL = entry - 15, TP = entry + 17
       Mod 3: SL = entry - 15, TP = entry + 18
     After each modify, verify the position's live SL/TP match what we
     requested (broker state, not just the OrderResult).
  6. Close the test position using the SAME pattern as the cleanup
     script: mt5.order_send with ORDER_FILLING_* from the symbol's
     filling_mode bitmask.
  7. Verify demo is clean (0 test positions remaining).
  8. Report per-step: timestamp, retcode, live SL/TP snapshot, PASS/FAIL.

ON ANY STEP FAILURE: still attempts cleanup before exiting.

USAGE

  python scripts/_investigations/flo352_live_validation.py
  python scripts/_investigations/flo352_live_validation.py --auto-clean-stale

Exit code:
  0 = all modify_position calls succeeded + clean exit
  1 = at least one modify_position failed (cleanup still attempted)
  2 = setup/preconditions failed (no test position opened)
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


TEST_COMMENT = "FLO352_LIVE_VALIDATION"
STALE_COMMENT_PREFIXES = ("FLO352_", "TEST_FLO348_")
NUM_MODIFIES = 3


def _ts() -> str:
    return datetime.utcnow().strftime("%H:%M:%S.%f")[:-3] + "Z"


def _print_step(label: str, status: str, detail: str = "") -> None:
    tag = {"PASS": "PASS", "FAIL": "FAIL", "INFO": "INFO", "ABORT": "ABORT"}.get(status, status)
    print(f"  [{_ts()}] [{tag}] {label}" + (f" — {detail}" if detail else ""))


def _check_bot_stopped() -> bool:
    pid_file = REPO_ROOT / "data" / "bot.pid"
    if not pid_file.exists():
        _print_step("bot.pid not present", "INFO")
        return True
    try:
        pid = int(pid_file.read_text().strip())
    except Exception:
        _print_step("bot.pid unreadable — assumed stale", "INFO")
        return True
    try:
        if sys.platform == "win32":
            import ctypes
            h = ctypes.windll.kernel32.OpenProcess(0x0400, False, pid)
            if h:
                ctypes.windll.kernel32.CloseHandle(h)
                _print_step(f"bot.pid={pid} is LIVE — stop the bot first", "ABORT")
                return False
            _print_step(f"bot.pid={pid} not live (stale pid file)", "INFO")
            return True
        else:
            os.kill(pid, 0)
            _print_step(f"bot.pid={pid} is LIVE — stop the bot first", "ABORT")
            return False
    except (ProcessLookupError, OSError):
        _print_step(f"bot.pid={pid} not live", "INFO")
        return True


def _resolve_filling_mode(symbol_info, mt5) -> int:
    mask = int(symbol_info.filling_mode)
    if mask & 2:
        return mt5.ORDER_FILLING_IOC
    if mask & 1:
        return mt5.ORDER_FILLING_FOK
    return mt5.ORDER_FILLING_RETURN


def _close_with_fill(ticket: int, volume: float, direction: int, mt5) -> tuple[bool, object]:
    """Close a position with correct type_filling. Returns (success, result)."""
    tick = mt5.symbol_info_tick("XAUUSD")
    si = mt5.symbol_info("XAUUSD")
    filling = _resolve_filling_mode(si, mt5)
    close_req = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": "XAUUSD",
        "position": ticket,
        "volume": volume,
        "type": mt5.ORDER_TYPE_SELL if direction == 0 else mt5.ORDER_TYPE_BUY,
        "price": tick.bid if direction == 0 else tick.ask,
        "deviation": 20,
        "magic": 234000,
        "comment": "flo352_live_validation_close",
        "type_filling": filling,
        "type_time": mt5.ORDER_TIME_GTC,
    }
    result = mt5.order_send(close_req)
    if result is None:
        return False, None
    return (result.retcode == mt5.TRADE_RETCODE_DONE), result


def _cleanup_test_positions(mt5, reason: str) -> int:
    """Whitelist-only cleanup; returns count closed. NEVER touches production."""
    positions = mt5.positions_get(symbol="XAUUSD") or []
    targets = [p for p in positions if any((p.comment or "").startswith(pfx)
                                           for pfx in STALE_COMMENT_PREFIXES)]
    if not targets:
        return 0
    print(f"  [cleanup:{reason}] closing {len(targets)} test positions")
    closed = 0
    for p in targets:
        ok, res = _close_with_fill(p.ticket, p.volume, p.type, mt5)
        if ok:
            print(f"    OK   #{p.ticket}  comment={p.comment!r}")
            closed += 1
        else:
            rc = res.retcode if res else None
            print(f"    FAIL #{p.ticket}  comment={p.comment!r}  retcode={rc}")
        time.sleep(0.2)
    return closed


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--auto-clean-stale", action="store_true",
                        help="Auto-close any FLO352_*/TEST_FLO348_* stale positions "
                             "before running validation.")
    args = parser.parse_args()

    print("FLO-352 live validation — executor.modify_position end-to-end")
    print("=" * 65)
    print(f"start: {datetime.utcnow().isoformat()}Z")

    # --- Precondition: bot stopped ---
    print("\n--- Precondition 1: bot stopped ---")
    if not _check_bot_stopped():
        return 2

    # --- Precondition: MT5 connectivity + demo account ---
    print("\n--- Precondition 2: MT5 + executor.connect() ---")
    from mt5_safe import mt5
    from executor import executor
    if not mt5.initialize():
        _print_step(f"mt5.initialize failed; last_error={mt5.last_error()}", "ABORT")
        return 2
    if not executor.connect():
        _print_step("executor.connect() returned False", "ABORT")
        mt5.shutdown()
        return 2
    ai = mt5.account_info()
    ti = mt5.terminal_info()
    _print_step(f"terminal={ti.name}  trade_allowed={ti.trade_allowed}", "INFO")
    _print_step(f"account login={ai.login} server={ai.server} balance=${ai.balance:.2f}", "INFO")
    _print_step(f"executor.connected={executor.is_connected()}", "INFO")

    # --- Precondition: demo clean (or auto-cleanable) ---
    print("\n--- Precondition 3: demo account clean ---")
    positions = mt5.positions_get(symbol="XAUUSD") or []
    stale = [p for p in positions if any((p.comment or "").startswith(pfx)
                                         for pfx in STALE_COMMENT_PREFIXES)]
    production = [p for p in positions if p not in stale]

    if production:
        _print_step(f"{len(production)} PRODUCTION positions present — refuse to run", "ABORT")
        for p in production:
            print(f"    #{p.ticket}  {'BUY' if p.type==0 else 'SELL'}  "
                  f"comment={p.comment!r}  vol={p.volume}")
        mt5.shutdown()
        return 2

    if stale:
        if args.auto_clean_stale:
            _print_step(f"auto-cleaning {len(stale)} stale test positions", "INFO")
            _cleanup_test_positions(mt5, "pre-flight")
            time.sleep(1)
            remaining = [p for p in (mt5.positions_get(symbol="XAUUSD") or [])
                         if any((p.comment or "").startswith(pfx) for pfx in STALE_COMMENT_PREFIXES)]
            if remaining:
                _print_step(f"{len(remaining)} stale positions still remaining", "ABORT")
                mt5.shutdown()
                return 2
            _print_step("stale cleaned", "PASS")
        else:
            _print_step(f"{len(stale)} stale test positions present — "
                        f"re-run with --auto-clean-stale", "ABORT")
            mt5.shutdown()
            return 2
    else:
        _print_step("0 open positions", "PASS")

    # --- OPEN ---
    print("\n--- Step 1: open test position via executor.execute_trade ---")
    tick = mt5.symbol_info_tick("XAUUSD")
    entry_ref = float(tick.ask)   # anchor all SL/TP to this
    sl_initial = round(entry_ref - 15.0, 2)
    tp_initial = round(entry_ref + 15.0, 2)
    _print_step(f"ref entry={entry_ref}  initial SL={sl_initial}  TP={tp_initial}", "INFO")

    t0 = time.time()
    open_result = executor.execute_trade(
        direction="BUY",
        lot_size=0.01,
        stop_loss=sl_initial,
        take_profit=tp_initial,
        comment=TEST_COMMENT,
    )
    open_ms = (time.time() - t0) * 1000

    if not getattr(open_result, "success", False):
        _print_step(f"execute_trade failed: {open_result}", "FAIL")
        _cleanup_test_positions(mt5, "open-fail")
        mt5.shutdown()
        return 2

    ticket = int(open_result.ticket)
    _print_step(f"opened ticket #{ticket}  elapsed={open_ms:.0f}ms", "PASS")

    # Confirm position exists in MT5 state
    time.sleep(0.5)
    pos_list = [p for p in (mt5.positions_get(symbol="XAUUSD") or [])
                if p.ticket == ticket]
    if not pos_list:
        _print_step(f"position #{ticket} not found in positions_get", "FAIL")
        _cleanup_test_positions(mt5, "post-open-missing")
        mt5.shutdown()
        return 2
    live_pos = pos_list[0]
    _print_step(f"live position: entry={live_pos.price_open}  "
                f"SL={live_pos.sl}  TP={live_pos.tp}", "INFO")

    # --- MODIFY loop ---
    modify_records: list[dict] = []
    all_modifies_ok = True

    for i in range(1, NUM_MODIFIES + 1):
        print(f"\n--- Step 2.{i}: executor.modify_position (call {i}/{NUM_MODIFIES}) ---")
        new_sl = sl_initial                    # hold constant
        new_tp = round(tp_initial + float(i), 2)   # widen TP by 1.0 units each call
        _print_step(f"requested SL={new_sl}  TP={new_tp}", "INFO")

        t1 = time.time()
        result = executor.modify_position(ticket, new_sl=new_sl, new_tp=new_tp)
        elapsed_ms = (time.time() - t1) * 1000
        success = getattr(result, "success", False)
        error_code = getattr(result, "error_code", None)
        error_msg = getattr(result, "error_message", None)

        # Verify broker-side state matches request
        time.sleep(0.3)
        live = [p for p in (mt5.positions_get(symbol="XAUUSD") or [])
                if p.ticket == ticket]
        live_sl = live[0].sl if live else None
        live_tp = live[0].tp if live else None

        step_passes = (
            success
            and error_code is None
            and live_sl is not None
            and abs(float(live_sl) - new_sl) < 0.01
            and abs(float(live_tp) - new_tp) < 0.01
        )

        detail = (f"success={success} err_code={error_code} err={error_msg!r}  "
                  f"live SL={live_sl}/expected {new_sl}  "
                  f"live TP={live_tp}/expected {new_tp}  "
                  f"elapsed={elapsed_ms:.0f}ms")
        _print_step(f"modify {i}", "PASS" if step_passes else "FAIL", detail)

        modify_records.append({
            "call": i,
            "success": success,
            "error_code": error_code,
            "error_message": error_msg,
            "requested_sl": new_sl, "requested_tp": new_tp,
            "live_sl": live_sl, "live_tp": live_tp,
            "elapsed_ms": elapsed_ms,
            "step_pass": step_passes,
        })
        if not step_passes:
            all_modifies_ok = False

        time.sleep(0.5)

    # --- CLOSE ---
    print("\n--- Step 3: close test position ---")
    live = [p for p in (mt5.positions_get(symbol="XAUUSD") or [])
            if p.ticket == ticket]
    if not live:
        _print_step(f"position #{ticket} already gone (broker-side close?)", "INFO")
    else:
        ok, result = _close_with_fill(ticket, live[0].volume, live[0].type, mt5)
        if ok:
            _print_step(f"closed at {result.price}  deal={result.deal}", "PASS")
        else:
            rc = result.retcode if result else None
            _print_step(f"close failed  retcode={rc}", "FAIL")
            # Try cleanup as last resort
            _cleanup_test_positions(mt5, "close-fail")

    # --- Post-verify ---
    print("\n--- Step 4: post-verify demo clean ---")
    time.sleep(1)
    final = [p for p in (mt5.positions_get(symbol="XAUUSD") or [])
             if (p.comment or "").startswith(TEST_COMMENT)]
    if final:
        _print_step(f"{len(final)} test positions still remain", "FAIL")
        for p in final:
            print(f"    #{p.ticket}")
    else:
        _print_step("0 test positions remain", "PASS")

    # --- Summary ---
    print("\n" + "=" * 65)
    print("SUMMARY")
    print("=" * 65)
    for rec in modify_records:
        status = "PASS" if rec["step_pass"] else "FAIL"
        print(f"  modify {rec['call']}/{NUM_MODIFIES}: {status}  "
              f"SL={rec['live_sl']}({rec['requested_sl']})  "
              f"TP={rec['live_tp']}({rec['requested_tp']})  "
              f"err={rec['error_message']!r}  elapsed={rec['elapsed_ms']:.0f}ms")

    all_pass = all_modifies_ok and not final
    print()
    if all_pass:
        print(f"OVERALL: PASS — executor.modify_position works live "
              f"({NUM_MODIFIES}/{NUM_MODIFIES} modifies succeeded, demo clean)")
    else:
        print("OVERALL: FAIL — see per-step details above")

    mt5.shutdown()
    return 0 if all_pass else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\ninterrupted")
        sys.exit(1)
    except Exception as e:
        import traceback
        traceback.print_exc()
        sys.exit(1)
