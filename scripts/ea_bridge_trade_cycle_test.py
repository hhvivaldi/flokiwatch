import os
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config


def _get_prices() -> tuple[float, float]:
    from executor import executor, connect_mt5, disconnect_mt5

    try:
        try:
            connect_mt5()
        except Exception:
            pass

        prices = executor.get_current_price()
        if not prices:
            raise RuntimeError("No MT5 prices available")
        bid, ask = prices
        return float(bid), float(ask)
    finally:
        try:
            disconnect_mt5()
        except Exception:
            pass


def main() -> int:
    from ea_bridge import write_signal

    bid, ask = _get_prices()
    entry = bid

    # Keep this modest so it closes quickly and still triggers monitor lifecycle.
    sl = entry + 30.0
    tp = entry - 30.0

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    comment = f"EA-TEST SELL cycle {now}"

    ok = write_signal(
        signal="SELL",
        sl=sl,
        tp=tp,
        lot_size=0.01,
        confidence=99.0,
        breakeven_trigger_pips=9999,
        trailing_trigger_pips=9999,
        trailing_distance_pips=9999,
        max_drawdown_pips=float(getattr(config, "MAX_POSITION_DRAWDOWN_PIPS", 1000)),
        comment=comment,
    )

    if not ok:
        raise SystemExit(2)

    time.sleep(120)

    ok2 = write_signal(
        signal="CLOSE",
        sl=sl,
        tp=tp,
        lot_size=0.01,
        confidence=99.0,
        breakeven_trigger_pips=9999,
        trailing_trigger_pips=9999,
        trailing_distance_pips=9999,
        max_drawdown_pips=float(getattr(config, "MAX_POSITION_DRAWDOWN_PIPS", 1000)),
        comment=comment,
    )

    if not ok2:
        raise SystemExit(3)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
