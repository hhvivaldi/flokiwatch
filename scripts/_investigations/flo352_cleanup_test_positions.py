"""FLO-352 recovery — close diagnostic-script leftover positions.

Context: flo352_modify_position_diagnosis.py (and minor reproducer scripts)
opened test positions on the demo account. The cleanup path used an
incomplete close request (missing `type_filling`), causing retcode 10030
(TRADE_RETCODE_INVALID_FILL). All close attempts printed
"last_error: (1, 'Success')" which referred to the MT5 BINDING state, NOT
the order outcome — misleading.

This script:
  1. Connects to MT5
  2. Queries ALL XAUUSD positions (not just by magic, to be thorough)
  3. Filters to positions whose comment matches a KNOWN TEST PREFIX
  4. Closes each with the filling mode required by the symbol
  5. Refuses to touch anything whose comment does NOT match a test prefix
  6. Reports per-position success/failure with retcode

Safe on any environment: only positions with the whitelisted test
comment prefixes are touched. Floki / production positions (comment
"floki_agent", "Agent-*", "Pending-*", "reconciled:*", anything else)
are NEVER closed by this script.

USAGE
  python scripts/_investigations/flo352_cleanup_test_positions.py

  Run with --dry-run to list what WOULD be closed without actually
  closing:

  python scripts/_investigations/flo352_cleanup_test_positions.py --dry-run
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


# Whitelist: only positions with these comment PREFIXES are closed.
# Any position whose comment doesn't match is considered production and
# left untouched.
TEST_COMMENT_PREFIXES = (
    "FLO352_",         # FLO-352 diagnostic scripts (DIAG, MINREPRO, B, C)
    "TEST_FLO348_",    # FLO-348 Test 2 real_order_test script
)


def _is_test_position(comment: str | None) -> bool:
    if not comment:
        return False
    return any(comment.startswith(p) for p in TEST_COMMENT_PREFIXES)


def _resolve_filling_mode(symbol_info) -> int:
    """Pick the correct ORDER_FILLING_* constant from the symbol's bitmask.

    filling_mode bitmask: 1=FOK, 2=IOC, 3=FOK|IOC, 4=RETURN, etc.
    Prefer IOC (most permissive); fall back to FOK; last resort RETURN.
    """
    import MetaTrader5 as mt5
    mask = int(symbol_info.filling_mode)
    if mask & 2:
        return mt5.ORDER_FILLING_IOC
    if mask & 1:
        return mt5.ORDER_FILLING_FOK
    return mt5.ORDER_FILLING_RETURN


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true",
                        help="List positions that would be closed; do not close.")
    args = parser.parse_args()

    import MetaTrader5 as mt5
    if not mt5.initialize():
        print(f"ABORT: mt5.initialize failed; last_error={mt5.last_error()}")
        return 2

    ai = mt5.account_info()
    print(f"account: login={ai.login} server={ai.server} balance=${ai.balance:.2f} equity=${ai.equity:.2f}")

    positions = mt5.positions_get(symbol="XAUUSD") or []
    print(f"\nTotal XAUUSD positions on demo: {len(positions)}")

    test_positions = []
    production_positions = []
    for p in positions:
        if _is_test_position(p.comment):
            test_positions.append(p)
        else:
            production_positions.append(p)

    print(f"  test positions (will be closed): {len(test_positions)}")
    print(f"  production positions (WILL NOT BE TOUCHED): {len(production_positions)}")

    if production_positions:
        print("\n  Production positions left untouched:")
        for p in production_positions:
            print(f"    #{p.ticket}  {'BUY' if p.type == 0 else 'SELL'}  "
                  f"vol={p.volume}  comment={p.comment!r}  profit=${p.profit:.2f}")

    if not test_positions:
        print("\nNo test positions to close. Exit.")
        mt5.shutdown()
        return 0

    print("\nTest positions to close:")
    for p in test_positions:
        direction = "BUY" if p.type == 0 else "SELL"
        print(f"  #{p.ticket}  {direction}  vol={p.volume}  @{p.price_open}  "
              f"comment={p.comment!r}  profit=${p.profit:.2f}")

    if args.dry_run:
        print("\n--dry-run specified: no positions closed.")
        mt5.shutdown()
        return 0

    symbol_info = mt5.symbol_info("XAUUSD")
    filling = _resolve_filling_mode(symbol_info)
    filling_name = {mt5.ORDER_FILLING_IOC: "IOC",
                    mt5.ORDER_FILLING_FOK: "FOK",
                    mt5.ORDER_FILLING_RETURN: "RETURN"}.get(filling, "?")
    print(f"\nUsing ORDER_FILLING_{filling_name} (symbol bitmask={symbol_info.filling_mode})")

    print("\nClosing test positions:")
    succeeded = 0
    failed: list[tuple[int, int, str]] = []

    for p in test_positions:
        tick = mt5.symbol_info_tick("XAUUSD")
        if tick is None:
            failed.append((p.ticket, -99, "no tick"))
            continue
        # Opposite side + opposite price
        if p.type == 0:  # BUY position → close by SELL at bid
            close_type = mt5.ORDER_TYPE_SELL
            price = tick.bid
        else:            # SELL position → close by BUY at ask
            close_type = mt5.ORDER_TYPE_BUY
            price = tick.ask

        close_req = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": "XAUUSD",
            "position": p.ticket,
            "volume": p.volume,
            "type": close_type,
            "price": price,
            "deviation": 20,
            "magic": int(p.magic) if p.magic else 234000,
            "comment": "flo352_cleanup",
            "type_filling": filling,
            "type_time": mt5.ORDER_TIME_GTC,
        }
        result = mt5.order_send(close_req)
        if result is None:
            err = mt5.last_error()
            print(f"  FAIL #{p.ticket}: order_send returned None, last_error={err}")
            failed.append((p.ticket, -1, f"None; {err}"))
            continue

        if result.retcode == mt5.TRADE_RETCODE_DONE:
            print(f"  OK   #{p.ticket}: closed at {result.price}  deal={result.deal}")
            succeeded += 1
        else:
            print(f"  FAIL #{p.ticket}: retcode={result.retcode}  comment={result.comment!r}")
            failed.append((p.ticket, result.retcode, result.comment))

        time.sleep(0.2)  # gentle rate-limit

    # Post-verify
    time.sleep(1.0)
    remaining = mt5.positions_get(symbol="XAUUSD") or []
    remaining_test = [p for p in remaining if _is_test_position(p.comment)]

    print(f"\n--- SUMMARY ---")
    print(f"attempted:  {len(test_positions)}")
    print(f"succeeded:  {succeeded}")
    print(f"failed:     {len(failed)}")
    if failed:
        print("failures:")
        for tk, rc, msg in failed:
            print(f"  #{tk}: retcode={rc} msg={msg!r}")
    print(f"remaining test positions: {len(remaining_test)}")
    for p in remaining_test:
        print(f"  #{p.ticket}  comment={p.comment!r}")

    if production_positions:
        print(f"\nPRODUCTION POSITIONS STILL OPEN (left untouched by design):")
        for p in production_positions:
            print(f"  #{p.ticket}  {'BUY' if p.type==0 else 'SELL'}  comment={p.comment!r}")

    mt5.shutdown()
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
