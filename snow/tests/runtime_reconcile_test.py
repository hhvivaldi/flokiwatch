"""FLO-379 — runtime in-loop reconciliation tests.

Pre-fix incident: PLAN-20260427-004's BE-locked SL fired broker-side
at 13:11:35 UTC. Snow loop kept the plan as ACTIVE for 2+ hours
because no in-loop pass reconciled the broker close. Floki, prompted
to refrain from duplicate plans while `list_active_plans` is non-empty,
silently throttled — zero submissions across 16 cycles. This module
verifies the new runtime pass closes that gap.

Coverage:
- ACTIVE → CLOSED for SL hit (DEAL_REASON_SL)
- ACTIVE → CLOSED for TP hit (DEAL_REASON_TP)
- ACTIVE → CLOSED for manual close (DEAL_REASON_CLIENT)
- closed_at uses BROKER deal time, not detection time (CTO directive)
- MT5 transient None → no transition, log warning
- MT5 disconnect (positions_get returns None) → skip pass cleanly
- DB read error → return empty summary, no raise
- Idempotent under repeated invocation (COALESCE protection)
- Position still open → no transition (control case)
- ACTIVE without ticket is SKIPPED at runtime (startup-only condition)
- Empty deals list → leave for retry (don't FAIL like startup does)
- fetch_deal_history fallback path used when MT5 quirk strikes
"""
from __future__ import annotations

import sqlite3
from copy import deepcopy
from typing import Any

import pytest

from snow import db as snow_db
from snow.schema import Plan, PlanStatus


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def snow_conn(tmp_path, monkeypatch):
    db_path = tmp_path / "runtime_reconcile_test.db"

    def _tmp_connect():
        conn = sqlite3.connect(str(db_path), timeout=5)
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.row_factory = sqlite3.Row
        return conn

    monkeypatch.setattr(snow_db, "_connect", _tmp_connect)
    snow_db.init_snow_tables()
    return db_path


_BASE_PLAN: dict[str, Any] = {
    "schema_version": 3,
    "id": "PLAN-20260427-100",
    "created_by": "floki",
    "created_at": "2026-04-27T08:00:00Z",
    "expires_at": "2026-04-27T16:00:00Z",
    "status": "pending",
    "analysis": {
        "thesis": "runtime reconcile test",
        "key_levels": [4700.0],
        "confidence": 60,
        "regime_assumed": "RANGING",
        "setup_type": "pullback_trend",
        "context_tags": {
            "trend": "trend_strong", "volatility": "high_vol",
            "htf": "HTF_aligned", "news_session": [],
        },
        "confidence_reason": "FLO-379 runtime reconciliation coverage spec",
    },
    "entry": {
        "direction": "SELL", "volume": 0.02,
        "conditions": [{"type": "price_above", "level": 4710.0}],
        "initial_sl": 4720.0, "initial_tp": 4690.0,
    },
    "management": [],
    "exit": [{"name": "fallback_target", "priority": 1, "conditions": [{"type": "profit_pips", "op": "above", "threshold": 9999}], "action": {"type": "close_full"}, "fires": "once"}],  # FLO-401 floor
    "emergency": {
        "max_loss_pips": 150, "max_duration_minutes": 480,
        "on_broker_error": "alert_floki",
    },
}


def _insert_active_with_ticket(plan_id: str, ticket: int):
    d = deepcopy(_BASE_PLAN)
    d["id"] = plan_id
    snow_db.insert_plan(Plan(**d))
    snow_db.update_plan_status(plan_id, PlanStatus.ACTIVE.value)
    snow_db.update_plan_trade_ticket(plan_id, ticket)


def _read_row(plan_id: str) -> dict:
    conn = snow_db._connect()
    try:
        r = conn.execute(
            "SELECT * FROM snow_plans WHERE id = ?", (plan_id,),
        ).fetchone()
        return dict(r) if r else {}
    finally:
        conn.close()


def _read_audit_events(plan_id: str) -> list[dict]:
    conn = snow_db._connect()
    try:
        rows = conn.execute(
            "SELECT * FROM snow_evaluations WHERE plan_id = ? "
            "ORDER BY evaluated_at ASC",
            (plan_id,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Fake MT5
# ---------------------------------------------------------------------------

class _FakeDeal:
    def __init__(self, *, position_id, entry, time_, price=4699.65,
                 volume=0.02, profit=-1.20, ticket=99000, magic=234000,
                 reason=4, symbol="XAUUSD"):
        self.position_id = position_id
        self.entry = entry  # 0=IN, 1=OUT
        self.time = int(time_)
        self.price = price
        self.volume = volume
        self.profit = profit
        self.ticket = ticket
        self.magic = magic
        self.reason = reason
        self.symbol = symbol
        self.commission = 0.0
        self.swap = 0.0


class FakeMT5:
    """Minimal MT5 stand-in for runtime-reconcile tests.

    Behaviors are configured at construction. positions_get can be
    overridden to return None (disconnect simulation) by passing
    `positions_disconnect=True`.
    """
    def __init__(self, *, positions=(), deals=(),
                 deal_history_returns_none=False,
                 positions_disconnect=False,
                 deals_position_filter_empty=False):
        self._positions = list(positions)
        self._deals = list(deals)
        self._deal_history_returns_none = deal_history_returns_none
        self._positions_disconnect = positions_disconnect
        # FLO-379: when the MT5 quirk strikes, position=ticket returns
        # raw deals that filter to empty (none with matching position_id).
        self._deals_position_filter_empty = deals_position_filter_empty

    def initialize(self): return True
    def shutdown(self): pass

    def positions_get(self, *args, **kwargs):
        if self._positions_disconnect:
            return None
        return tuple(self._positions)

    def history_deals_get(self, *args, **kwargs):
        if self._deal_history_returns_none:
            return None
        if self._deals_position_filter_empty and "position" in kwargs:
            # Return non-empty list with WRONG position_ids — caller's
            # filter will drop them all. This triggers the FLO-379
            # fallback (re-query without `position` kwarg).
            decoy = _FakeDeal(
                position_id=99999, entry=1, time_=1714000000,
            )
            return (decoy,)
        return tuple(self._deals)


class _FakePosition:
    def __init__(self, ticket, magic=234000):
        self.ticket = ticket
        self.magic = magic
        self.symbol = "XAUUSD"


# ---------------------------------------------------------------------------
# Core: ACTIVE → CLOSED via runtime pass
# ---------------------------------------------------------------------------

class TestRuntimeReconcileActiveToClosed:
    def _broker_close_unix(self) -> int:
        # 2026-04-27T13:11:35Z = 1777986695 (UTC unix seconds).
        import datetime as dt
        return int(
            dt.datetime(
                2026, 4, 27, 13, 11, 35,
                tzinfo=dt.timezone.utc,
            ).timestamp()
        )

    def test_sl_hit_transitions_to_closed_with_broker_time(
        self, snow_conn, monkeypatch,
    ):
        from snow import runtime_reconcile as rr
        from snow import recovery as snow_recovery
        monkeypatch.setattr(snow_recovery._time, "sleep", lambda _s: None)

        _insert_active_with_ticket("PLAN-20260427-101", 555_000)
        close_unix = self._broker_close_unix()
        mt5 = FakeMT5(
            positions=(),  # No live positions — broker SL fired
            deals=(
                _FakeDeal(position_id=555_000, entry=0,
                          time_=close_unix - 1800,  # IN deal, 30 min earlier
                          price=4699.04, profit=0.0, reason=3),
                _FakeDeal(position_id=555_000, entry=1,
                          time_=close_unix,
                          price=4699.65, profit=-1.20, reason=4),  # SL
            ),
        )

        summary = rr.reconcile_runtime(mt5_proxy=mt5, magic=234000)

        assert summary.active_to_closed == 1
        assert summary.active_left_for_retry == 0
        row = _read_row("PLAN-20260427-101")
        assert row["status"] == "closed"
        # CTO directive: closed_at = BROKER time, not utc_iso() now.
        assert row["closed_at"] == "2026-04-27T13:11:35Z"

    def test_tp_hit_transitions_to_closed(self, snow_conn, monkeypatch):
        from snow import runtime_reconcile as rr
        from snow import recovery as snow_recovery
        monkeypatch.setattr(snow_recovery._time, "sleep", lambda _s: None)

        _insert_active_with_ticket("PLAN-20260427-102", 555_001)
        close_unix = self._broker_close_unix()
        mt5 = FakeMT5(
            positions=(),
            deals=(
                _FakeDeal(position_id=555_001, entry=0,
                          time_=close_unix - 600,
                          price=4699.04, profit=0.0, reason=3),
                _FakeDeal(position_id=555_001, entry=1,
                          time_=close_unix,
                          price=4690.0, profit=18.0, reason=5),  # TP
            ),
        )
        summary = rr.reconcile_runtime(mt5_proxy=mt5, magic=234000)
        assert summary.active_to_closed == 1
        assert _read_row("PLAN-20260427-102")["status"] == "closed"

    def test_manual_close_transitions_to_closed(
        self, snow_conn, monkeypatch,
    ):
        from snow import runtime_reconcile as rr
        from snow import recovery as snow_recovery
        monkeypatch.setattr(snow_recovery._time, "sleep", lambda _s: None)

        _insert_active_with_ticket("PLAN-20260427-103", 555_002)
        close_unix = self._broker_close_unix()
        mt5 = FakeMT5(
            positions=(),
            deals=(
                _FakeDeal(position_id=555_002, entry=0,
                          time_=close_unix - 300,
                          price=4699.04, profit=0.0, reason=3),
                _FakeDeal(position_id=555_002, entry=1,
                          time_=close_unix,
                          price=4699.20, profit=-0.32,
                          reason=2),  # CLIENT (manual close)
            ),
        )
        summary = rr.reconcile_runtime(mt5_proxy=mt5, magic=234000)
        assert summary.active_to_closed == 1
        assert _read_row("PLAN-20260427-103")["status"] == "closed"

    def test_audit_row_uses_runtime_event_name(
        self, snow_conn, monkeypatch,
    ):
        """FLO-379: the audit row must use event='runtime_active_to_closed',
        distinguishable from recovery's 'recovery_active_to_closed'."""
        from snow import runtime_reconcile as rr
        from snow import recovery as snow_recovery
        monkeypatch.setattr(snow_recovery._time, "sleep", lambda _s: None)

        _insert_active_with_ticket("PLAN-20260427-104", 555_003)
        close_unix = self._broker_close_unix()
        mt5 = FakeMT5(
            positions=(),
            deals=(
                _FakeDeal(position_id=555_003, entry=1,
                          time_=close_unix, profit=-1.20),
            ),
        )
        rr.reconcile_runtime(mt5_proxy=mt5, magic=234000)

        events = [
            e for e in _read_audit_events("PLAN-20260427-104")
            if e["event"] == "runtime_active_to_closed"
        ]
        assert len(events) == 1
        snap = events[0]["conditions_snapshot"]
        # Snapshot is JSON; must include broker_close_time_utc.
        import json
        parsed = json.loads(snap)
        assert parsed["ticket"] == 555_003
        assert parsed["broker_close_time_utc"] == "2026-04-27T13:11:35Z"


# ---------------------------------------------------------------------------
# Defensive: never raises, never mass-FAILs
# ---------------------------------------------------------------------------

class TestRuntimeReconcileDefensive:
    def test_position_still_open_no_transition(self, snow_conn):
        """Control case: when MT5 still shows the position, do nothing."""
        from snow import runtime_reconcile as rr

        _insert_active_with_ticket("PLAN-20260427-200", 555_100)
        mt5 = FakeMT5(positions=(_FakePosition(555_100),))
        summary = rr.reconcile_runtime(mt5_proxy=mt5, magic=234000)
        assert summary.active_to_closed == 0
        assert _read_row("PLAN-20260427-200")["status"] == "active"

    def test_mt5_disconnect_skips_pass_cleanly(self, snow_conn):
        """positions_get returning None must NOT mass-FAIL plans."""
        from snow import runtime_reconcile as rr

        _insert_active_with_ticket("PLAN-20260427-201", 555_101)
        mt5 = FakeMT5(positions_disconnect=True)
        summary = rr.reconcile_runtime(mt5_proxy=mt5, magic=234000)
        assert summary.active_to_closed == 0
        assert _read_row("PLAN-20260427-201")["status"] == "active"

    def test_deal_history_unavailable_left_for_retry(
        self, snow_conn, monkeypatch,
    ):
        """Transient MT5 deal-history error → leave plan ACTIVE."""
        from snow import runtime_reconcile as rr
        from snow import recovery as snow_recovery
        monkeypatch.setattr(snow_recovery._time, "sleep", lambda _s: None)

        _insert_active_with_ticket("PLAN-20260427-202", 555_102)
        mt5 = FakeMT5(positions=(), deal_history_returns_none=True)
        summary = rr.reconcile_runtime(mt5_proxy=mt5, magic=234000)
        assert summary.active_to_closed == 0
        assert summary.active_left_for_retry == 1
        assert _read_row("PLAN-20260427-202")["status"] == "active"

    def test_empty_deals_left_for_retry(self, snow_conn, monkeypatch):
        """Empty deal list (could be MT5 lag) → DON'T fail; leave for
        retry. Startup recovery's `position_vanished` FAIL is too
        aggressive at runtime."""
        from snow import runtime_reconcile as rr
        from snow import recovery as snow_recovery
        monkeypatch.setattr(snow_recovery._time, "sleep", lambda _s: None)

        _insert_active_with_ticket("PLAN-20260427-203", 555_103)
        mt5 = FakeMT5(positions=(), deals=())
        summary = rr.reconcile_runtime(mt5_proxy=mt5, magic=234000)
        assert summary.active_to_closed == 0
        assert summary.active_left_for_retry == 1
        assert _read_row("PLAN-20260427-203")["status"] == "active"

    def test_active_without_ticket_is_skipped(self, snow_conn):
        """ACTIVE without ticket is a startup-only data-loss
        condition — runtime must not touch it."""
        from snow import runtime_reconcile as rr

        d = deepcopy(_BASE_PLAN)
        d["id"] = "PLAN-20260427-204"
        snow_db.insert_plan(Plan(**d))
        snow_db.update_plan_status("PLAN-20260427-204", PlanStatus.ACTIVE.value)
        # No ticket assigned.

        mt5 = FakeMT5(positions=())
        summary = rr.reconcile_runtime(mt5_proxy=mt5, magic=234000)
        assert summary.plans_checked == 0
        assert _read_row("PLAN-20260427-204")["status"] == "active"

    def test_coalesce_protects_closed_at_against_double_stamp(
        self, snow_conn, monkeypatch,
    ):
        """Direct test of mark_plan_terminal's COALESCE — bypass the
        candidate filter so we exercise the actual concurrency
        guarantee. If startup recovery and the runtime pass race on
        the same plan, the first stamp must win."""
        from snow import runtime_reconcile as rr
        from snow import recovery as snow_recovery
        monkeypatch.setattr(snow_recovery._time, "sleep", lambda _s: None)

        _insert_active_with_ticket("PLAN-20260427-220", 555_120)
        close_unix = TestRuntimeReconcileActiveToClosed()._broker_close_unix()
        deals = (_FakeDeal(position_id=555_120, entry=1, time_=close_unix),)

        # First call directly into the per-plan helper.
        summary = rr.RuntimeReconcileSummary()
        mt5 = FakeMT5(positions=(), deals=deals)
        rr._runtime_reconcile_one(
            "PLAN-20260427-220", 555_120, summary, mt5_proxy=mt5,
        )
        first_closed_at = _read_row("PLAN-20260427-220")["closed_at"]
        assert first_closed_at == "2026-04-27T13:11:35Z"

        # Second call on the SAME plan with a DIFFERENT broker close
        # time. COALESCE on closed_at must keep the first value.
        later_unix = close_unix + 600
        deals2 = (_FakeDeal(position_id=555_120, entry=1, time_=later_unix),)
        mt5_2 = FakeMT5(positions=(), deals=deals2)
        rr._runtime_reconcile_one(
            "PLAN-20260427-220", 555_120, summary, mt5_proxy=mt5_2,
        )
        assert _read_row("PLAN-20260427-220")["closed_at"] == first_closed_at

    def test_idempotent_under_repeat_call(self, snow_conn, monkeypatch):
        """Public-API idempotency: calling reconcile_runtime twice
        no-ops the second time because the candidate filter excludes
        already-closed plans (`closed_at IS NULL` in the query)."""
        from snow import runtime_reconcile as rr
        from snow import recovery as snow_recovery
        monkeypatch.setattr(snow_recovery._time, "sleep", lambda _s: None)

        _insert_active_with_ticket("PLAN-20260427-205", 555_104)
        close_unix = TestRuntimeReconcileActiveToClosed()._broker_close_unix()
        mt5 = FakeMT5(
            positions=(),
            deals=(
                _FakeDeal(position_id=555_104, entry=1, time_=close_unix),
            ),
        )
        rr.reconcile_runtime(mt5_proxy=mt5, magic=234000)
        first_closed_at = _read_row("PLAN-20260427-205")["closed_at"]
        # Second call. Because status went terminal, the candidate
        # filter excludes the plan now (closed_at is set). But even
        # if a stale row sneaked through, COALESCE would protect.
        rr.reconcile_runtime(mt5_proxy=mt5, magic=234000)
        second_closed_at = _read_row("PLAN-20260427-205")["closed_at"]
        assert first_closed_at == second_closed_at == "2026-04-27T13:11:35Z"

    def test_db_read_error_returns_empty_summary(
        self, snow_conn, monkeypatch,
    ):
        from snow import runtime_reconcile as rr

        def _boom(*a, **k):
            raise RuntimeError("simulated DB read failure")
        monkeypatch.setattr(snow_db, "list_plans_by_status", _boom)
        mt5 = FakeMT5(positions=())
        summary = rr.reconcile_runtime(mt5_proxy=mt5, magic=234000)
        assert summary.plans_checked == 0
        assert summary.active_to_closed == 0


# ---------------------------------------------------------------------------
# fetch_deal_history fallback (FLO-379 MT5 quirk)
# ---------------------------------------------------------------------------

class TestFLO380BrokerNaiveDatetimes:
    """FLO-380 regression: fetch_deal_history must convert tz-aware
    UTC datetimes to broker-naive before calling history_deals_get.
    Pre-FLO-380 caused MT5 to mis-classify the query window,
    dropping recent deals — canary PLAN-20260427-005 returned 0
    matches at restart even though deals existed."""

    def test_history_deals_get_receives_naive_datetime_not_tz_aware(
        self, snow_conn, monkeypatch,
    ):
        """The PRIMARY fix: history_deals_get must receive naive
        datetimes. A fake proxy records what it gets and asserts."""
        from snow import recovery as snow_recovery
        monkeypatch.setattr(snow_recovery._time, "sleep", lambda _s: None)

        captured = {"date_from": None, "date_to": None}

        class _FakeTick:
            time = int(__import__("time").time()) + 10800  # +3h offset

        class _Proxy:
            def symbol_info_tick(self, symbol):
                return _FakeTick()
            def history_deals_get(self, df, dt, **kw):
                captured["date_from"] = df
                captured["date_to"] = dt
                return ()  # empty is fine for this assertion

        snow_recovery.fetch_deal_history(
            555_888, mt5_proxy=_Proxy(), max_attempts=1,
        )
        assert captured["date_from"] is not None
        assert captured["date_to"] is not None
        # Pre-FLO-380: tz-aware (had .tzinfo). Post-fix: naive.
        assert captured["date_from"].tzinfo is None, (
            "fetch_deal_history must pass NAIVE datetime to MT5; "
            f"got tzinfo={captured['date_from'].tzinfo!r}"
        )
        assert captured["date_to"].tzinfo is None

    def test_simulated_mt5_tz_quirk_now_resolves(
        self, snow_conn, monkeypatch,
    ):
        """End-to-end: a fake MT5 that returns deals ONLY when
        called with broker-naive datetimes (modeling the real
        production quirk). Pre-FLO-380 fetch_deal_history would
        return [] for these; post-fix it returns the deals."""
        from snow import recovery as snow_recovery
        monkeypatch.setattr(snow_recovery._time, "sleep", lambda _s: None)

        ticket = 555_889
        real_deal = _FakeDeal(
            position_id=ticket, entry=1, time_=1714000000,
        )

        class _FakeTick:
            time = int(__import__("time").time()) + 10800

        class _QuirkyProxy:
            """Simulates real MT5: tz-aware datetimes return wrong
            deals (or none); naive datetimes return correctly."""
            def symbol_info_tick(self, symbol):
                return _FakeTick()
            def history_deals_get(self, df, dt, **kw):
                # If caller passed tz-aware, simulate MT5 dropping
                # the matching deal entirely.
                if df.tzinfo is not None or dt.tzinfo is not None:
                    return ()
                return (real_deal,)

        result = snow_recovery.fetch_deal_history(
            ticket, mt5_proxy=_QuirkyProxy(), max_attempts=1,
        )
        assert result is not None
        assert len(result) == 1
        assert int(result[0].position_id) == ticket

    def test_proxy_without_symbol_info_tick_falls_back_to_naive_utc(
        self, snow_conn, monkeypatch,
    ):
        """Defensive: if proxy has no symbol_info_tick (or it raises),
        the helper falls back to naive UTC. Window is misclassified
        by the broker offset, but at least no tz-aware leaks to MT5."""
        from snow import recovery as snow_recovery
        monkeypatch.setattr(snow_recovery._time, "sleep", lambda _s: None)

        captured = {"date_from": None}

        class _MinimalProxy:
            def symbol_info_tick(self, symbol):
                raise RuntimeError("simulated tick unavailable")
            def history_deals_get(self, df, dt, **kw):
                captured["date_from"] = df
                return ()

        snow_recovery.fetch_deal_history(
            555_890, mt5_proxy=_MinimalProxy(), max_attempts=1,
        )
        assert captured["date_from"] is not None
        assert captured["date_from"].tzinfo is None  # naive

    def test_helper_offset_math_matches_canonical_pattern(self):
        """Direct unit test of the helper math. Should mirror
        mfe_backfill._utc_to_broker_naive output for the same
        offset."""
        import datetime as dt
        from snow import recovery as snow_recovery

        class _FakeTick:
            # Broker is +10800s ahead of real UTC.
            pass

        class _Proxy:
            def symbol_info_tick(self, symbol):
                t = _FakeTick()
                t.time = int(__import__("time").time()) + 10800
                return t

        utc = dt.datetime(2026, 4, 27, 14, 29, 42, tzinfo=dt.timezone.utc)
        result = snow_recovery._utc_to_broker_naive(utc, _Proxy())
        assert result.tzinfo is None
        # Expected: broker_unix = utc_unix + 10800 → fromtimestamp
        # in the test box's local timezone equals the broker wall
        # clock. We can't assert exact wall time without knowing
        # the test box tz, but we can assert the unix delta.
        delta_s = int(result.timestamp()) - int(utc.timestamp())
        assert delta_s == 10800, (
            f"Expected +10800s broker offset; got {delta_s}s"
        )


class TestFetchDealHistoryFallback:
    def test_position_filter_empty_triggers_no_hint_fallback(
        self, snow_conn, monkeypatch,
    ):
        """When MT5's `position=ticket` filter returns deals that all
        filter to empty (the PLAN-20260427-004 quirk), the fallback
        re-queries without the hint."""
        from snow import recovery as snow_recovery
        monkeypatch.setattr(snow_recovery._time, "sleep", lambda _s: None)

        # Build a fake that returns DECOY deals when called WITH
        # `position=...` (filter to 0) but the REAL deal when called
        # WITHOUT it.
        real_deal = _FakeDeal(
            position_id=555_900, entry=1, time_=1714000000,
        )
        decoy = _FakeDeal(
            position_id=99999, entry=1, time_=1714000000,
        )

        class _Proxy:
            def history_deals_get(self, df, dt, **kw):
                if "position" in kw:
                    return (decoy,)  # raw=1, filtered=0
                return (real_deal,)  # fallback path returns the real deal

        proxy = _Proxy()
        result = snow_recovery.fetch_deal_history(555_900, mt5_proxy=proxy)
        assert result is not None
        assert len(result) == 1
        assert int(getattr(result[0], "position_id", -1)) == 555_900
