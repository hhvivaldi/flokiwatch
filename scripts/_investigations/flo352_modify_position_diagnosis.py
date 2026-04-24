"""FLO-352 — modify_position regression diagnosis.

Systematically reproduces the "Modify position failed: Unknown error (-1)"
that emerged after FLO-348 (commit d2d0ed8). Runs a 5-configuration test
matrix against a live DEMO position, captures exact failure point, reports.

SAFE TO RUN while bot is STOPPED.

WHAT IT DOES

1. mt5.initialize() via RAW module (no FLO-348 code)
2. Open a tiny test BUY 0.01 lot on XAUUSD with comment 'FLO352_DIAG'
3. Run 5 modify_position attempts, each capturing:
     - result (full repr)
     - result.retcode
     - mt5.last_error() IMMEDIATELY after the call
     - elapsed time
   Configurations:
     A. Raw mt5.order_send, main thread (baseline — must work)
     B. Through mt5_safe proxy, main thread
     C. Through executor.modify_position (decorator path), main thread
     D. Through executor.modify_position, WORKER thread
     E. Raw mt5.order_send AFTER proxy has been used (catch state pollution)
4. Close test position
5. Print matrix summary

Each config uses distinct SL values so broker doesn't reject "no change":
  SL_0 = bid - 100 pts   (initial)
  SL_A = bid - 110 pts
  SL_B = bid - 120 pts
  SL_C = bid - 130 pts
  SL_D = bid - 140 pts
  SL_E = bid - 150 pts

USAGE
  python scripts/_investigations/flo352_modify_position_diagnosis.py

Exit code: 0 if diagnosis completed (regardless of pass/fail per config),
           2 if setup failed and we couldn't run tests.
"""

from __future__ import annotations

import sys
import threading
import time
import traceback
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


TEST_COMMENT = "FLO352_DIAG"
TEST_VOLUME = 0.01


def _banner(msg: str) -> None:
    print("\n" + "=" * 60)
    print(msg)
    print("=" * 60)


def _print_result(label: str, result, mt5_err, elapsed_ms: float) -> dict:
    """Capture + print one modify attempt's outcome."""
    if result is None:
        retcode = None
        comment = None
    else:
        retcode = getattr(result, "retcode", None)
        comment = getattr(result, "comment", None)
    record = {
        "label": label,
        "result_is_None": result is None,
        "retcode": retcode,
        "comment": comment,
        "last_error": mt5_err,
        "elapsed_ms": elapsed_ms,
        "full_repr": repr(result),
    }
    # Accept BOTH shapes: raw MT5 OrderSendResult (has retcode) OR executor
    # OrderResult (has success attribute). They're different return types.
    if result is None:
        status = "FAIL"
    elif hasattr(result, "success"):
        status = "PASS" if getattr(result, "success", False) else "FAIL"
    elif retcode == 10009:
        status = "PASS"
    else:
        status = "FAIL"
    print(f"  [{status}] {label}")
    print(f"        result is None:  {result is None}")
    print(f"        retcode:         {retcode}")
    print(f"        comment:         {comment!r}")
    print(f"        last_error:      {mt5_err}")
    print(f"        elapsed:         {elapsed_ms:.1f} ms")
    print(f"        repr:            {repr(result)[:200]}")
    return record


def main() -> int:
    import MetaTrader5 as raw_mt5

    _banner("FLO-352 diagnosis — modify_position regression")

    if not raw_mt5.initialize():
        print(f"ABORT: raw mt5.initialize failed; last_error={raw_mt5.last_error()}")
        return 2

    ti = raw_mt5.terminal_info()
    ai = raw_mt5.account_info()
    print(f"terminal: {ti.name if ti else '?'}  connected={ti.connected if ti else '?'}")
    print(f"account:  login={ai.login if ai else '?'}  server={ai.server if ai else '?'}  bal=${ai.balance if ai else '?'}")
    print(f"main thread ident: {threading.get_ident()}")

    tick = raw_mt5.symbol_info_tick("XAUUSD")
    if tick is None or tick.bid <= 0:
        print("ABORT: no live tick for XAUUSD")
        raw_mt5.shutdown()
        return 2
    bid = float(tick.bid)
    ask = float(tick.ask)
    print(f"bid={bid} ask={ask} spread={(ask-bid)/0.1:.1f}p")

    # ---- Pre-flight: ensure no leftover diagnostic positions ----
    all_pos = raw_mt5.positions_get(symbol="XAUUSD") or []
    leftover = [p for p in all_pos if (p.comment or "").startswith(TEST_COMMENT)]
    # FLO-352 lesson: close requests MUST include type_filling or MT5 returns
    # retcode 10030 (TRADE_RETCODE_INVALID_FILL). Pick from symbol bitmask.
    _si = raw_mt5.symbol_info("XAUUSD")
    _fill_mask = int(_si.filling_mode) if _si else 2
    CLOSE_FILLING = (raw_mt5.ORDER_FILLING_IOC if _fill_mask & 2
                     else raw_mt5.ORDER_FILLING_FOK if _fill_mask & 1
                     else raw_mt5.ORDER_FILLING_RETURN)

    if leftover:
        print(f"CLEANUP: {len(leftover)} leftover {TEST_COMMENT} positions — closing first")
        for p in leftover:
            req = {
                "action": raw_mt5.TRADE_ACTION_DEAL,
                "symbol": "XAUUSD",
                "position": p.ticket,
                "volume": p.volume,
                "type": raw_mt5.ORDER_TYPE_SELL if p.type == 0 else raw_mt5.ORDER_TYPE_BUY,
                "price": raw_mt5.symbol_info_tick("XAUUSD").bid if p.type == 0 else raw_mt5.symbol_info_tick("XAUUSD").ask,
                "deviation": 20,
                "type_filling": CLOSE_FILLING,
                "type_time": raw_mt5.ORDER_TIME_GTC,
            }
            r = raw_mt5.order_send(req)
            ok = (r is not None and r.retcode == raw_mt5.TRADE_RETCODE_DONE)
            print(f"  leftover #{p.ticket} close: retcode={r.retcode if r else None}  "
                  f"{'OK' if ok else 'FAIL — position may remain open'}")

    # ---- Open test position ----
    _banner("Opening test position")
    SL_0 = bid - 10.0  # 100 points below
    TP_0 = bid + 10.0
    open_req = {
        "action": raw_mt5.TRADE_ACTION_DEAL,
        "symbol": "XAUUSD",
        "volume": TEST_VOLUME,
        "type": raw_mt5.ORDER_TYPE_BUY,
        "price": ask,
        "sl": SL_0,
        "tp": TP_0,
        "deviation": 20,
        "magic": 234000,
        "comment": TEST_COMMENT,
        "type_time": raw_mt5.ORDER_TIME_GTC,
        "type_filling": raw_mt5.ORDER_FILLING_IOC,
    }
    open_res = raw_mt5.order_send(open_req)
    if open_res is None or open_res.retcode != raw_mt5.TRADE_RETCODE_DONE:
        print(f"ABORT: open failed. retcode={open_res.retcode if open_res else None} "
              f"last_error={raw_mt5.last_error()}")
        raw_mt5.shutdown()
        return 2
    ticket = int(open_res.order)
    # For actual position ticket, query positions_get — order.ticket in recent MT5 may == position.ticket
    time.sleep(0.5)  # let position register
    positions = raw_mt5.positions_get(symbol="XAUUSD") or []
    test_pos = [p for p in positions if (p.comment or "") == TEST_COMMENT]
    if not test_pos:
        print(f"ABORT: opened order {ticket} but no position found")
        raw_mt5.shutdown()
        return 2
    ticket = test_pos[0].ticket
    print(f"opened ticket {ticket} @ {test_pos[0].price_open}  SL={test_pos[0].sl}  TP={test_pos[0].tp}")

    records: list[dict] = []

    # ---- Config A: raw mt5.order_send, main thread ----
    _banner("Config A — RAW mt5.order_send, main thread (baseline)")
    SL_A = bid - 11.0
    req_A = {
        "action": raw_mt5.TRADE_ACTION_SLTP,
        "symbol": "XAUUSD",
        "position": ticket,
        "sl": SL_A,
        "tp": TP_0,
    }
    t0 = time.time()
    res_A = raw_mt5.order_send(req_A)
    err_A = raw_mt5.last_error()
    records.append(_print_result("A: raw main-thread", res_A, err_A, (time.time()-t0)*1000))

    time.sleep(0.3)

    # ---- Config B: through mt5_safe proxy, main thread ----
    _banner("Config B — mt5_safe proxy, main thread")
    # Import proxy fresh
    from mt5_safe import mt5 as safe_mt5, mt5_lock
    SL_B = bid - 12.0
    req_B = {
        "action": safe_mt5.TRADE_ACTION_SLTP,
        "symbol": "XAUUSD",
        "position": ticket,
        "sl": SL_B,
        "tp": TP_0,
    }
    t0 = time.time()
    res_B = safe_mt5.order_send(req_B)
    err_B = safe_mt5.last_error()
    records.append(_print_result("B: proxy main-thread", res_B, err_B, (time.time()-t0)*1000))

    time.sleep(0.3)

    # ---- Config C: executor.modify_position (decorator path), main thread ----
    _banner("Config C — executor.modify_position (FLO-348 decorator), main thread")
    # Need executor connected
    from executor import executor, executor_lock
    if not executor.is_connected():
        if not executor.connect():
            print("  SETUP FAIL: executor.connect() False")
            records.append({"label": "C: executor main-thread", "setup_fail": True})
        else:
            print(f"  executor.connected = True")
    SL_C = bid - 13.0
    t0 = time.time()
    try:
        res_C = executor.modify_position(ticket, new_sl=SL_C, new_tp=TP_0)
    except Exception as e:
        res_C = None
        print(f"  EXCEPTION: {e}")
    err_C = safe_mt5.last_error()
    records.append(_print_result("C: executor main-thread",
                                 res_C, err_C, (time.time()-t0)*1000))

    time.sleep(0.3)

    # ---- Config D: executor.modify_position, WORKER thread ----
    _banner("Config D — executor.modify_position, WORKER thread")
    SL_D = bid - 14.0
    worker_result = {"res": None, "err": None, "elapsed": 0.0, "thread_id": None}

    def worker():
        worker_result["thread_id"] = threading.get_ident()
        t0 = time.time()
        try:
            worker_result["res"] = executor.modify_position(ticket, new_sl=SL_D, new_tp=TP_0)
        except Exception as e:
            print(f"  worker EXCEPTION: {e}")
        worker_result["err"] = safe_mt5.last_error()
        worker_result["elapsed"] = (time.time() - t0) * 1000

    t = threading.Thread(target=worker, name="DiagWorker")
    t.start()
    t.join(timeout=30.0)
    print(f"  worker thread ident: {worker_result['thread_id']}  (main={threading.get_ident()})")
    records.append(_print_result("D: executor worker-thread",
                                 worker_result["res"], worker_result["err"],
                                 worker_result["elapsed"]))

    time.sleep(0.3)

    # ---- Config E: RAW mt5.order_send AFTER proxy use ----
    _banner("Config E — RAW mt5.order_send (post-proxy-use; state pollution check)")
    SL_E = bid - 15.0
    req_E = {
        "action": raw_mt5.TRADE_ACTION_SLTP,
        "symbol": "XAUUSD",
        "position": ticket,
        "sl": SL_E,
        "tp": TP_0,
    }
    t0 = time.time()
    res_E = raw_mt5.order_send(req_E)
    err_E = raw_mt5.last_error()
    records.append(_print_result("E: raw post-proxy main-thread",
                                 res_E, err_E, (time.time()-t0)*1000))

    # ---- Cleanup: close test position ----
    _banner("Cleanup")
    positions_now = raw_mt5.positions_get(symbol="XAUUSD") or []
    test_pos = [p for p in positions_now if (p.comment or "") == TEST_COMMENT]
    for p in test_pos:
        tick_now = raw_mt5.symbol_info_tick("XAUUSD")
        close_req = {
            "action": raw_mt5.TRADE_ACTION_DEAL,
            "symbol": "XAUUSD",
            "position": p.ticket,
            "volume": p.volume,
            "type": raw_mt5.ORDER_TYPE_SELL if p.type == 0 else raw_mt5.ORDER_TYPE_BUY,
            "price": tick_now.bid if p.type == 0 else tick_now.ask,
            "deviation": 20,
            "type_filling": CLOSE_FILLING,
            "type_time": raw_mt5.ORDER_TIME_GTC,
        }
        r = raw_mt5.order_send(close_req)
        ok = (r is not None and r.retcode == raw_mt5.TRADE_RETCODE_DONE)
        print(f"  close #{p.ticket}: retcode={r.retcode if r else None}  "
              f"{'OK' if ok else 'FAIL — run scripts/_investigations/flo352_cleanup_test_positions.py'}")

    # ---- Matrix summary ----
    _banner("DIAGNOSIS MATRIX")
    for rec in records:
        if rec.get("setup_fail"):
            print(f"  {rec['label']:<45}  SETUP FAIL")
            continue
        # Matches the shape check in _print_result
        if rec["result_is_None"]:
            status = "FAIL"
        elif "success=True" in rec["full_repr"]:
            status = "PASS"
        elif rec["retcode"] == 10009:
            status = "PASS"
        else:
            status = "FAIL"
        print(f"  {rec['label']:<45}  {status:>4}  retcode={rec['retcode']}  "
              f"last_error={rec['last_error']}")

    raw_mt5.shutdown()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\ninterrupted")
        sys.exit(1)
    except Exception as e:
        traceback.print_exc()
        sys.exit(1)
