"""FLO-382 — diagnostic instrumentation unit tests.

Verifies the three emitters produce well-formed log lines and never
propagate failures to their callers.

  * D1: snow.plan.recipe_pulled
  * D2: snow.trade.scratch_pattern
  * D3: snow.trade.volume_audit
"""
from __future__ import annotations

import json
import logging as stdlib_logging
from types import SimpleNamespace

import pytest

from snow.instrumentation import (
    categories_for_setup_type,
    emit_recipe_pulled,
    emit_scratch_and_volume_audit,
)


# ---------------------------------------------------------------------------
# D1 — recipe_pulled emit
# ---------------------------------------------------------------------------

class TestD1RecipePulled:
    def test_matched_when_pulled_category_aligns_with_setup_type(self, caplog):
        with caplog.at_level(stdlib_logging.INFO):
            emit_recipe_pulled(
                plan_id="PLAN-20260428-T01",
                recipe_pulls=[{"ts": "2026-04-28T08:00:00Z",
                               "category": "trend", "count": 4}],
                final_setup_type="pullback_trend",
            )
        msgs = [r.getMessage() for r in caplog.records]
        line = next(m for m in msgs if "snow.plan.recipe_pulled" in m)
        assert "match_status=matched" in line
        assert "final_setup_type=pullback_trend" in line
        assert "recipe_pulls_count=1" in line
        assert "recipe_categories_pulled=[trend]" in line

    def test_mismatched_when_pulled_category_does_not_cover_setup_type(
        self, caplog,
    ):
        with caplog.at_level(stdlib_logging.INFO):
            emit_recipe_pulled(
                plan_id="PLAN-20260428-T02",
                recipe_pulls=[{"ts": "2026-04-28T08:00:00Z",
                               "category": "range", "count": 2}],
                final_setup_type="pullback_trend",
            )
        line = next(r.getMessage() for r in caplog.records
                    if "snow.plan.recipe_pulled" in r.getMessage())
        assert "match_status=mismatched" in line
        assert "recipe_categories_pulled=[range]" in line

    def test_no_pull_when_buffer_empty(self, caplog):
        with caplog.at_level(stdlib_logging.INFO):
            emit_recipe_pulled(
                plan_id="PLAN-20260428-T03",
                recipe_pulls=[],
                final_setup_type="pullback_trend",
            )
        line = next(r.getMessage() for r in caplog.records
                    if "snow.plan.recipe_pulled" in r.getMessage())
        assert "match_status=no_pull" in line
        assert "recipe_pulls_count=0" in line

    def test_no_setup_type_when_plan_has_none(self, caplog):
        with caplog.at_level(stdlib_logging.INFO):
            emit_recipe_pulled(
                plan_id="PLAN-20260428-T04",
                recipe_pulls=[{"ts": "2026-04-28T08:00:00Z",
                               "category": "trend", "count": 4}],
                final_setup_type=None,
            )
        line = next(r.getMessage() for r in caplog.records
                    if "snow.plan.recipe_pulled" in r.getMessage())
        assert "match_status=no_setup_type" in line
        assert "final_setup_type=null" in line

    def test_categories_for_setup_type_is_data_driven(self):
        # `pullback_trend` is referenced in trend-category recipes per
        # the recipe book itself — verify the data-driven mapping
        # surfaces it (don't pin to a hand-coded table).
        cats = categories_for_setup_type("pullback_trend")
        assert "trend" in cats, (
            f"data-driven mapping must include 'trend' for "
            f"pullback_trend (recipe book ground truth); got {cats}"
        )

    def test_categories_for_unknown_setup_type_is_empty(self):
        cats = categories_for_setup_type("definitely_not_a_real_setup_type")
        assert cats == []

    def test_paired_hedge_two_submits_both_see_cycle_pulls(self, caplog):
        """Regression guard for advisor-flagged blocking issue.

        Floki sometimes submits two plans in the same second (paired
        hedge: BUY plan + SELL plan covering both breakout dirs).
        Both submits MUST see the cycle's recipe pulls — the recency
        window must NOT clear the buffer per submit, otherwise the
        second plan emits match_status=no_pull spuriously.
        """
        cycle_pulls = [
            {"ts": "2026-04-28T08:00:00Z", "category": "trend", "count": 4},
            {"ts": "2026-04-28T08:00:01Z",
             "category": "risk_management", "count": 4},
        ]
        with caplog.at_level(stdlib_logging.INFO):
            emit_recipe_pulled(
                plan_id="PLAN-20260428-PH1",
                recipe_pulls=cycle_pulls,
                final_setup_type="paired_hedge",
            )
            # Same buffer, second submit in the same cycle.
            emit_recipe_pulled(
                plan_id="PLAN-20260428-PH2",
                recipe_pulls=cycle_pulls,
                final_setup_type="paired_hedge",
            )
        msgs = [r.getMessage() for r in caplog.records
                if "snow.plan.recipe_pulled" in r.getMessage()]
        assert len(msgs) == 2
        # Both must show non-zero pulls and matched status.
        for m in msgs:
            assert "recipe_pulls_count=2" in m, (
                f"paired-hedge submit must see cycle's 2 pulls; got: {m}"
            )
            assert "match_status=matched" in m, (
                f"paired_hedge → risk_management mapping; got: {m}"
            )


# ---------------------------------------------------------------------------
# D2 + D3 helpers
# ---------------------------------------------------------------------------

def _make_deal(*, entry, time_, price, volume=0.02, profit=-0.20,
               reason=2, sl=0.0):
    """Compact MT5 deal stand-in. `entry`: 0=IN, 1=OUT."""
    return SimpleNamespace(
        entry=entry, time=time_, price=price, volume=volume,
        profit=profit, reason=reason, sl=sl,
    )


def _make_plan_row(*, plan_id, planned_volume=0.02, symbol="XAUUSD",
                   entered_at="2026-04-28T07:30:00Z"):
    plan_json = {
        "id": plan_id,
        "symbol": symbol,
        "entry": {
            "direction": "BUY", "volume": planned_volume,
            "conditions": [], "initial_sl": 4680.0, "initial_tp": 4720.0,
        },
        "analysis": {"setup_type": "pullback_trend"},
    }
    return {
        "id": plan_id,
        "plan_json": json.dumps(plan_json),
        "symbol": symbol,
        "entered_at": entered_at,
    }


# ---------------------------------------------------------------------------
# D2 — scratch_pattern emit
# ---------------------------------------------------------------------------

class TestD2ScratchPattern:
    def test_broker_sl_classified_when_deal_reason_is_sl(
        self, caplog, monkeypatch,
    ):
        # Monkeypatch MT5 copy_rates_range to fail so we exercise the
        # MFE-null fallback path simultaneously (covers two D2
        # acceptance criteria in one assertion set).
        from snow import instrumentation as instr

        def _fail_copy_rates(*a, **kw):
            raise RuntimeError("MT5 unavailable in test")
        # Patch via the import path the helper uses
        import mt5_safe
        monkeypatch.setattr(mt5_safe.mt5, "copy_rates_range", _fail_copy_rates,
                            raising=False)
        monkeypatch.setattr(mt5_safe.mt5, "DEAL_REASON_SL", 4, raising=False)
        monkeypatch.setattr(mt5_safe.mt5, "DEAL_REASON_TP", 3, raising=False)
        monkeypatch.setattr(mt5_safe.mt5, "DEAL_REASON_CLIENT", 5,
                            raising=False)
        monkeypatch.setattr(mt5_safe.mt5, "DEAL_REASON_EXPERT", 2,
                            raising=False)

        in_deal = _make_deal(entry=0, time_=1714290600, price=4690.0,
                             volume=0.02, profit=0.0, reason=0, sl=0.0)
        out_deal = _make_deal(entry=1, time_=1714291200, price=4689.5,
                              volume=0.02, profit=-0.10,
                              reason=4,  # broker SL
                              sl=4690.0)  # SL == entry → BE was locked

        with caplog.at_level(stdlib_logging.INFO):
            emit_scratch_and_volume_audit(
                plan_id="PLAN-20260428-T11",
                ticket=99001,
                plan_row=_make_plan_row(plan_id="PLAN-20260428-T11"),
                in_deal=in_deal,
                close_deals=[out_deal],
                open_price=4690.0,
                vw_close_price=4689.5,
                direction_sign=1,
                outcome_pips=-5.0,
                pip_size=0.1,
                close_time_iso="2026-04-28T07:40:00Z",
            )
        line = next(r.getMessage() for r in caplog.records
                    if "snow.trade.scratch_pattern" in r.getMessage())
        assert "close_reason=broker_sl" in line
        assert "raw_deal_reason=4" in line
        assert "be_was_locked=true" in line
        assert "mfe_during_trade=null" in line
        assert "mfe_query_status=copy_rates_range_failed" in line

    def test_expert_unattributed_when_no_snow_trigger_match(
        self, caplog, monkeypatch,
    ):
        import mt5_safe
        monkeypatch.setattr(mt5_safe.mt5, "DEAL_REASON_SL", 4, raising=False)
        monkeypatch.setattr(mt5_safe.mt5, "DEAL_REASON_TP", 3, raising=False)
        monkeypatch.setattr(mt5_safe.mt5, "DEAL_REASON_CLIENT", 5,
                            raising=False)
        monkeypatch.setattr(mt5_safe.mt5, "DEAL_REASON_EXPERT", 2,
                            raising=False)

        # Stub list_triggers to return no matching close-action rows.
        from snow import instrumentation as instr
        from snow import db as snow_db
        monkeypatch.setattr(snow_db, "list_triggers", lambda **kw: [])

        in_deal = _make_deal(entry=0, time_=1714290600, price=4690.0,
                             reason=0)
        out_deal = _make_deal(entry=1, time_=1714291200, price=4691.0,
                              reason=2,  # EXPERT
                              sl=4670.0)  # not at entry — no BE lock

        with caplog.at_level(stdlib_logging.INFO):
            emit_scratch_and_volume_audit(
                plan_id="PLAN-20260428-T12",
                ticket=99002,
                plan_row=_make_plan_row(plan_id="PLAN-20260428-T12"),
                in_deal=in_deal,
                close_deals=[out_deal],
                open_price=4690.0,
                vw_close_price=4691.0,
                direction_sign=1,
                outcome_pips=10.0,
                pip_size=0.1,
                close_time_iso="2026-04-28T07:40:00Z",
            )
        line = next(r.getMessage() for r in caplog.records
                    if "snow.trade.scratch_pattern" in r.getMessage())
        assert "close_reason=expert_unattributed" in line
        assert "be_was_locked=false" in line


# ---------------------------------------------------------------------------
# D3 — volume_audit emit
# ---------------------------------------------------------------------------

class TestD3VolumeAudit:
    def test_volume_match_emits_mismatch_false(self, caplog, monkeypatch):
        import mt5_safe
        monkeypatch.setattr(mt5_safe.mt5, "DEAL_REASON_EXPERT", 2,
                            raising=False)
        monkeypatch.setattr(mt5_safe.mt5, "copy_rates_range",
                            lambda *a, **kw: None, raising=False)
        from snow import db as snow_db
        monkeypatch.setattr(snow_db, "list_triggers", lambda **kw: [])

        in_deal = _make_deal(entry=0, time_=1714290600, price=4690.0,
                             volume=0.02, reason=0)
        out_deal = _make_deal(entry=1, time_=1714291200, price=4691.0,
                              volume=0.02, reason=2, sl=4670.0)
        with caplog.at_level(stdlib_logging.INFO):
            emit_scratch_and_volume_audit(
                plan_id="PLAN-20260428-T21", ticket=99003,
                plan_row=_make_plan_row(
                    plan_id="PLAN-20260428-T21", planned_volume=0.02,
                ),
                in_deal=in_deal, close_deals=[out_deal],
                open_price=4690.0, vw_close_price=4691.0,
                direction_sign=1, outcome_pips=10.0, pip_size=0.1,
                close_time_iso="2026-04-28T07:40:00Z",
            )
        line = next(r.getMessage() for r in caplog.records
                    if "snow.trade.volume_audit" in r.getMessage())
        assert "planned_volume=0.02" in line
        assert "actual_volume=0.02" in line
        assert "mismatch=false" in line

    def test_volume_mismatch_emits_true(self, caplog, monkeypatch):
        import mt5_safe
        monkeypatch.setattr(mt5_safe.mt5, "DEAL_REASON_EXPERT", 2,
                            raising=False)
        monkeypatch.setattr(mt5_safe.mt5, "copy_rates_range",
                            lambda *a, **kw: None, raising=False)
        from snow import db as snow_db
        monkeypatch.setattr(snow_db, "list_triggers", lambda **kw: [])

        in_deal = _make_deal(entry=0, time_=1714290600, price=4690.0,
                             volume=0.01, reason=0)  # ACTUAL 0.01
        out_deal = _make_deal(entry=1, time_=1714291200, price=4691.0,
                              volume=0.01, reason=2, sl=4670.0)
        with caplog.at_level(stdlib_logging.INFO):
            emit_scratch_and_volume_audit(
                plan_id="PLAN-20260428-T22", ticket=99004,
                plan_row=_make_plan_row(
                    plan_id="PLAN-20260428-T22",
                    planned_volume=0.02,  # PLANNED 0.02
                ),
                in_deal=in_deal, close_deals=[out_deal],
                open_price=4690.0, vw_close_price=4691.0,
                direction_sign=1, outcome_pips=10.0, pip_size=0.1,
                close_time_iso="2026-04-28T07:40:00Z",
            )
        line = next(r.getMessage() for r in caplog.records
                    if "snow.trade.volume_audit" in r.getMessage())
        assert "mismatch=true" in line


# ---------------------------------------------------------------------------
# Failure-path resilience — emit must never propagate exceptions
# ---------------------------------------------------------------------------

class TestEmitFailureResilience:
    def test_recipe_pulled_emit_swallows_exceptions(self, caplog, monkeypatch):
        # Force categories_for_setup_type to raise via book corruption.
        from snow import recipe_book as rb

        def _broken(*a, **kw):
            raise RuntimeError("recipe book corrupted")

        monkeypatch.setattr(rb, "get_recipes_by_category", _broken)
        # Should NOT raise; emit either silently falls through or
        # warns. Either is acceptable — caller is unaffected.
        emit_recipe_pulled(
            plan_id="PLAN-20260428-T31",
            recipe_pulls=[{"ts": "now", "category": "trend", "count": 1}],
            final_setup_type="pullback_trend",
        )
        # No assertion on log content — only that no exception propagated.
