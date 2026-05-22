"""Regression test for the snow.recovery `pos.open_price` -> `pos.price_open` fix.

The MT5 TradePosition namedtuple exposes `price_open` (NOT `open_price` — that's
the monitor.py wrapper's field). recovery.py reconciles RAW MT5 positions, so it
must read `price_open`. Before the fix, _seed_tracker raised
`AttributeError: 'TradePosition' object has no attribute 'open_price'`
(snow.recovery.tracker_seed_failed) and orphaned trades got no Snow management.

Standalone. Run: python test_recovery_price_open_fix.py
"""
import json

import snow.recovery as R


class _RawMT5Pos:
    """Mimics an MT5 TradePosition: has `price_open`, deliberately NO `open_price`."""
    __slots__ = ("price_open", "ticket")

    def __init__(self, price_open, ticket=123):
        self.price_open = price_open
        self.ticket = ticket


class _FakeTracker:
    def __init__(self):
        self.seeded = []

    def seed(self, plan_id, price, direction):
        self.seeded.append((plan_id, price, direction))


def test_seed_tracker_reads_price_open():
    pos = _RawMT5Pos(4500.0)
    assert not hasattr(pos, "open_price"), "raw MT5 pos must NOT have open_price (bug attr)"
    row = {"plan_json": json.dumps({"entry": {"direction": "BUY"}})}
    tracker = _FakeTracker()
    summary = R.ReconcileSummary()

    R._seed_tracker(tracker, "PLAN-X", row, pos, summary)  # would AttributeError pre-fix

    assert len(tracker.seeded) == 1, f"tracker not seeded: {tracker.seeded}"
    plan_id, price, direction = tracker.seeded[0]
    assert plan_id == "PLAN-X" and price == 4500.0, tracker.seeded
    assert direction is not None, "direction should resolve from plan_json"
    assert summary.tracker_reseeds == 1, summary.tracker_reseeds
    print(f"PASS test_seed_tracker_reads_price_open (seeded {price} dir={direction})")


def test_no_open_price_attr_access_remains():
    import inspect
    src = inspect.getsource(R)
    assert "pos.open_price" not in src, "stale pos.open_price still present in recovery"
    assert "pos.price_open" in src, "recovery should read pos.price_open"
    print("PASS test_no_open_price_attr_access_remains")


if __name__ == "__main__":
    test_seed_tracker_reads_price_open()
    test_no_open_price_attr_access_remains()
    print("\nRECOVERY FIX VERIFIED")
