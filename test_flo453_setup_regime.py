"""FLO-453 — setup-regime matrix + thesis-break exit tests + counterfactual.
Standalone. Run: python test_flo453_setup_regime.py
"""
import re
import json
import sqlite3
from types import SimpleNamespace

from snow.validator import (
    _check_setup_regime_gate, _check_thesis_break_exit, SETUP_REGIME_MATRIX,
)


def _plan_sr(setup):
    return SimpleNamespace(id="X", analysis=SimpleNamespace(setup_type=setup))


def _cont(*types):
    return SimpleNamespace(conditions=[SimpleNamespace(type=t) for t in types])


def _plan_tb(setup, direction, mgmt=(), exit=()):
    return SimpleNamespace(id="X", analysis=SimpleNamespace(setup_type=setup),
                           entry=SimpleNamespace(direction=direction),
                           management=list(mgmt), exit=list(exit))


# ---- Matrix gate (4 cases) ----
def test_matrix_cases():
    assert _check_setup_regime_gate(_plan_sr("continuation_momentum"), {"adx": 20, "adx_rising": True}), "cont+ADX20 must REJECT"
    assert _check_setup_regime_gate(_plan_sr("continuation_momentum"), {"adx": 25, "adx_rising": False}), "cont+ADX25+not-rising must REJECT (rising required)"
    assert _check_setup_regime_gate(_plan_sr("continuation_momentum"), {"adx": 25, "adx_rising": True}) == [], "cont+ADX25+rising must ALLOW"
    assert _check_setup_regime_gate(_plan_sr("pullback_trend"), {"adx": 20, "adx_rising": False}) == [], "pullback+ADX20 must ALLOW"
    assert _check_setup_regime_gate(_plan_sr("structural_bounce"), {"adx": 30, "adx_rising": False}), "bounce+ADX30 must REJECT (max 25)"
    assert _check_setup_regime_gate(_plan_sr("continuation_momentum"), None) == [], "no ctx must fail-open"
    print("PASS test_matrix_cases (cont/ADX20 REJECT, cont/25+rising ALLOW, pullback/20 ALLOW, bounce/30 REJECT, none fail-open)")


# ---- Thesis-break exit (3 cases) ----
def test_thesis_break_cases():
    sell_no = _plan_tb("continuation_momentum", "SELL", exit=[_cont("duration_exceeds")], mgmt=[_cont("mfe_reached")])
    assert _check_thesis_break_exit(sell_no), "SELL continuation without price_above must REJECT"
    sell_yes = _plan_tb("continuation_momentum", "SELL", exit=[_cont("price_above")])
    assert _check_thesis_break_exit(sell_yes) == [], "SELL with price_above must ALLOW"
    buy_no = _plan_tb("continuation_momentum", "BUY", exit=[_cont("duration_exceeds")])
    assert _check_thesis_break_exit(buy_no), "BUY continuation without price_below must REJECT"
    buy_yes = _plan_tb("continuation_momentum", "BUY", mgmt=[_cont("price_below")])
    assert _check_thesis_break_exit(buy_yes) == [], "BUY with price_below (in mgmt) must ALLOW"
    pull = _plan_tb("pullback_trend", "SELL", exit=[_cont("duration_exceeds")])
    assert _check_thesis_break_exit(pull) == [], "non-momentum setup is exempt"
    print("PASS test_thesis_break_cases (SELL no-price_above REJECT, with ALLOW, BUY no-price_below REJECT, pullback exempt)")


# ---- Counterfactual ----
def counterfactual():
    c = sqlite3.connect("data/history.db"); c.row_factory = sqlite3.Row
    ids = ["PLAN-20260518-004", "PLAN-20260519-001", "PLAN-20260520-006",
           "PLAN-20260521-001", "PLAN-20260520-005", "PLAN-20260521-003"]
    outc = {"PLAN-20260518-004": "+66 WIN", "PLAN-20260519-001": "+20 WIN",
            "PLAN-20260520-006": "+23 win", "PLAN-20260521-001": "-48 LOSS",
            "PLAN-20260520-005": "-? LOSS", "PLAN-20260521-003": "-48 LOSS"}
    # Known ADX per plan (from each plan's regime/thesis at author time; mixed TF).
    known_adx = {"PLAN-20260518-004": 43.0, "PLAN-20260519-001": 41.9,
                 "PLAN-20260520-006": 19.7, "PLAN-20260521-001": 27.7,
                 "PLAN-20260520-005": 23.9, "PLAN-20260521-003": 20.2}
    print("\n=== FLO-453 SETUP-REGIME COUNTERFACTUAL (known author-time ADX) ===")
    print(f"{'plan':22} {'setup':22} {'adx':>5} {'reality':9} matrix")
    for pid in ids:
        r = c.execute("SELECT plan_json FROM snow_plans WHERE id=?", (pid,)).fetchone()
        setup = json.loads(r["plan_json"])["analysis"].get("setup_type")
        adx = known_adx.get(pid)
        # generous: assume rising, so the table reflects the ADX-window decision
        errs = _check_setup_regime_gate(_plan_sr(setup), {"adx": adx, "adx_rising": True})
        print(f"{pid:22} {str(setup):22} {adx:>5} {outc.get(pid,'?'):9} {'REJECT' if errs else 'ALLOW'}")
    c.close()
    print("Target: PLAN-...21-003 (continuation_momentum, ADX 20) -> REJECT. Watch for FALSE rejects of\n"
          "winning pullback_trend trades if their thesis ADX > 40 (pullback max_adx) — flag for tuning.")


if __name__ == "__main__":
    test_matrix_cases()
    test_thesis_break_cases()
    counterfactual()
    print("\nALL FLO-453 TESTS PASSED")
