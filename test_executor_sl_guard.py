"""Standalone verification for FLO-419 executor SL guard.

Run with `python test_executor_sl_guard.py`. Exits non-zero on failure.
Mocks the mt5 module so no MetaTrader is required. Tests the universal
monotonic-SL guard at executor.modify_position() that catches loosening
attempts from any caller (Snow, Qwen, Monitor, EA Bridge).
"""
from __future__ import annotations

import sys
from types import SimpleNamespace
from unittest.mock import patch, MagicMock


def _make_executor():
    """Build a real MT5Executor instance with mt5 mocked. Patches BEFORE
    import so mt5_safe's proxy sees our fake. Returns (executor, mt5_mock)."""
    fake_mt5 = MagicMock()
    fake_mt5.POSITION_TYPE_BUY = 0
    fake_mt5.POSITION_TYPE_SELL = 1
    fake_mt5.TRADE_ACTION_SLTP = 6
    fake_mt5.TRADE_RETCODE_DONE = 10009
    fake_mt5.order_send.return_value = SimpleNamespace(retcode=10009, deal=1, order=1)
    fake_mt5.symbol_info.return_value = SimpleNamespace(digits=2, point=0.01, trade_tick_size=0.01)
    fake_mt5.account_info.return_value = SimpleNamespace(balance=2000.0)
    fake_mt5.initialize.return_value = True
    fake_mt5.terminal_info.return_value = SimpleNamespace(connected=True)

    with patch.dict(sys.modules, {"MetaTrader5": fake_mt5}):
        for mod in ["mt5_safe", "executor"]:
            sys.modules.pop(mod, None)
        from executor import MT5Executor
        ex = MT5Executor()
        ex.connected = True  # bypass is_connected MT5 init check
        ex.dry_run = False    # ensure guard path runs
        return ex, fake_mt5


def _set_position(fake_mt5, *, ticket: int, ptype: int, sl: float, tp: float = 0.0):
    pos = SimpleNamespace(
        ticket=ticket, type=ptype, sl=sl, tp=tp,
        symbol="XAUUSDm", volume=0.02, price_open=4600.0,
    )
    fake_mt5.positions_get.return_value = [pos]


def assert_eq(actual, expected, label):
    if actual != expected:
        print(f"FAIL [{label}]: expected {expected!r}, got {actual!r}")
        sys.exit(1)
    print(f"PASS [{label}]")


def test_buy_loosen_rejected():
    """BUY with SL=4710, attempt to move SL down to 4708 (loosen). Expect rejection."""
    ex, mt5m = _make_executor()
    _set_position(mt5m, ticket=111, ptype=0, sl=4710.0)
    result = ex.modify_position(111, new_sl=4708.0)
    assert_eq(result.success, False, "buy loosen rejected: success=False")
    assert_eq(result.error_code, -5, "buy loosen rejected: error_code=-5")
    assert "SL_GUARD" in (result.error_message or ""), \
        f"buy loosen rejected: error_message contains SL_GUARD; got {result.error_message!r}"
    # mt5.order_send must NOT have been called
    assert mt5m.order_send.call_count == 0, \
        f"buy loosen rejected: order_send must not fire; called {mt5m.order_send.call_count} times"
    print("PASS [buy_loosen_rejected: order_send not called]")


def test_buy_tighten_allowed():
    """BUY with SL=4710, move SL up to 4712 (tighten). Expect success."""
    ex, mt5m = _make_executor()
    _set_position(mt5m, ticket=111, ptype=0, sl=4710.0, tp=4730.0)
    result = ex.modify_position(111, new_sl=4712.0)
    assert_eq(result.success, True, "buy tighten allowed: success=True")
    assert_eq(mt5m.order_send.call_count, 1, "buy tighten allowed: order_send called once")


def test_sell_loosen_rejected():
    """SELL with SL=4720, attempt to move SL up to 4722 (loosen). Expect rejection."""
    ex, mt5m = _make_executor()
    _set_position(mt5m, ticket=222, ptype=1, sl=4720.0)
    result = ex.modify_position(222, new_sl=4722.0)
    assert_eq(result.success, False, "sell loosen rejected: success=False")
    assert "SL_GUARD" in (result.error_message or ""), \
        f"sell loosen rejected: error_message contains SL_GUARD; got {result.error_message!r}"
    assert mt5m.order_send.call_count == 0, \
        "sell loosen rejected: order_send must not fire"


def test_sell_tighten_allowed():
    """SELL with SL=4720, move SL down to 4718 (tighten). Expect success."""
    ex, mt5m = _make_executor()
    _set_position(mt5m, ticket=222, ptype=1, sl=4720.0, tp=4690.0)
    result = ex.modify_position(222, new_sl=4718.0)
    assert_eq(result.success, True, "sell tighten allowed: success=True")
    assert_eq(mt5m.order_send.call_count, 1, "sell tighten allowed: order_send called once")


def test_first_sl_set_allowed_buy():
    """BUY with current_sl=0 (no prior SL), move SL anywhere. Bypass guard."""
    ex, mt5m = _make_executor()
    _set_position(mt5m, ticket=333, ptype=0, sl=0.0, tp=4730.0)
    result = ex.modify_position(333, new_sl=4550.0)  # very wide
    assert_eq(result.success, True, "first sl set buy: success=True")


def test_first_sl_set_allowed_sell():
    """SELL with current_sl=0, allow any new value."""
    ex, mt5m = _make_executor()
    _set_position(mt5m, ticket=444, ptype=1, sl=0.0, tp=4690.0)
    result = ex.modify_position(444, new_sl=4900.0)
    assert_eq(result.success, True, "first sl set sell: success=True")


def test_equal_sl_allowed():
    """new_sl == current_sl is a no-op write (e.g. clamped trail). Allowed."""
    ex, mt5m = _make_executor()
    _set_position(mt5m, ticket=555, ptype=0, sl=4710.0, tp=4730.0)
    result = ex.modify_position(555, new_sl=4710.0)
    assert_eq(result.success, True, "equal sl allowed: success=True")


def test_tp_only_modify_skips_sl_guard():
    """If new_sl is None and only new_tp is provided, guard does not fire."""
    ex, mt5m = _make_executor()
    _set_position(mt5m, ticket=666, ptype=1, sl=4720.0, tp=4690.0)
    result = ex.modify_position(666, new_tp=4685.0)
    assert_eq(result.success, True, "tp-only modify: success=True")


if __name__ == "__main__":
    test_buy_loosen_rejected()
    test_buy_tighten_allowed()
    test_sell_loosen_rejected()
    test_sell_tighten_allowed()
    test_first_sl_set_allowed_buy()
    test_first_sl_set_allowed_sell()
    test_equal_sl_allowed()
    test_tp_only_modify_skips_sl_guard()
    print("\nAll FLO-419 executor SL guard tests passed.")
