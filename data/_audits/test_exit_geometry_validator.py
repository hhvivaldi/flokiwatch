"""Sandbox demo of the proposed _check_exit_geometry_vs_sl validator
function. Runs the 6 test cases the plan describes AND replays the
real closed plans from the last-10 audit through the new check, so
the CEO sees actual rejection messages on real data before approving.
Modifies no production files."""
import sys, json, sqlite3
sys.path.insert(0, '.')

# --- Proposed validator function (paste candidate for snow/validator.py) ---

def _check_exit_geometry_vs_sl(plan):
    """For exit contingencies that use price-side triggers (price_above /
    price_below), reject when the trigger is positioned beyond the
    broker SL — the broker SL fires first, the exit never reaches.

    Rule:
      BUY plan + exit price_below level  -> level MUST be > initial_sl
      SELL plan + exit price_above level -> level MUST be < initial_sl
      BUY+price_above and SELL+price_below are TP-side triggers
        (no SL-ordering constraint).
    """
    errors = []
    direction = plan["entry"]["direction"]
    sl = float(plan["entry"]["initial_sl"])

    for ei, ex in enumerate(plan.get("exit", []) or []):
        name = ex.get("name", f"exit[{ei}]")
        for ci, c in enumerate(ex.get("conditions", []) or []):
            ctype = c.get("type")
            if ctype not in ("price_above", "price_below"):
                continue  # non-geometric leg — evaluator handles separately

            level = float(c.get("level"))

            # Match invalidation-side triggers per direction.
            if direction == "BUY" and ctype == "price_below":
                if level <= sl:
                    errors.append(
                        f"exit[{ei}] ({name!r}): price_below {level} is "
                        f"AT OR BELOW the SL {sl}. For a BUY plan, the "
                        f"broker SL fires when price drops to {sl}; this "
                        f"exit's trigger at {level} would never be reached "
                        f"BEFORE the broker SL. Set the exit level ABOVE "
                        f"the SL (typical: 1-2 USD above SL = 10-20 pips "
                        f"buffer) so the exit fires first, giving Snow the "
                        f"chance to close on thesis break before the broker "
                        f"hits SL. Or remove this exit if the broker SL is "
                        f"the intended invalidation level."
                    )
            elif direction == "SELL" and ctype == "price_above":
                if level >= sl:
                    errors.append(
                        f"exit[{ei}] ({name!r}): price_above {level} is "
                        f"AT OR ABOVE the SL {sl}. For a SELL plan, the "
                        f"broker SL fires when price rises to {sl}; this "
                        f"exit's trigger at {level} would never be reached "
                        f"BEFORE the broker SL. Set the exit level BELOW "
                        f"the SL (typical: 1-2 USD below SL = 10-20 pips "
                        f"buffer) so the exit fires first, giving Snow the "
                        f"chance to close on thesis break before the broker "
                        f"hits SL. Or remove this exit if the broker SL is "
                        f"the intended invalidation level."
                    )
            # BUY+price_above and SELL+price_below: TP-side, no constraint.
    return errors


# --- Test fixtures + runner ---

def make_plan(direction, sl, tp, exits, entry_price=None):
    return {
        "entry": {
            "direction": direction,
            "initial_sl": sl,
            "initial_tp": tp,
            "entry_price": entry_price,
        },
        "exit": exits,
    }


def run_test(name, plan, expect_reject, must_contain=None):
    errors = _check_exit_geometry_vs_sl(plan)
    rejected = bool(errors)
    pass_fail = "PASS" if rejected == expect_reject else "FAIL"
    if pass_fail == "PASS" and must_contain:
        for s in must_contain:
            if s not in (errors[0] if errors else ""):
                pass_fail = f"FAIL (missing '{s}' in error)"
                break
    print(f"[{pass_fail}] {name}")
    if errors:
        for e in errors:
            print(f"      ERROR: {e}")
    elif expect_reject:
        print(f"      (expected reject but got no errors)")
    print()
    return pass_fail.startswith("PASS")


def main():
    print("=" * 80)
    print("PART 1 — Synthetic test cases (the 6 cases proposed in the plan)")
    print("=" * 80 + "\n")

    results = []

    # 1. BUY invalidation BELOW SL — REJECT
    plan = make_plan("BUY", sl=4543.0, tp=4605.0, exits=[
        {"name": "thesis_invalidation_sweep_failed",
         "conditions": [{"type": "price_below", "level": 4525.0}]}
    ])
    results.append(run_test(
        "1. test_buy_invalidation_below_sl_rejected",
        plan, expect_reject=True,
        must_contain=["4525", "4543", "AT OR BELOW", "BUY"],
    ))

    # 2. BUY invalidation ABOVE SL — ACCEPT
    plan = make_plan("BUY", sl=4543.0, tp=4605.0, exits=[
        {"name": "thesis_invalidation",
         "conditions": [{"type": "price_below", "level": 4555.0}]}
    ])
    results.append(run_test(
        "2. test_buy_invalidation_above_sl_accepted",
        plan, expect_reject=False,
    ))

    # 3. SELL invalidation ABOVE SL — REJECT
    plan = make_plan("SELL", sl=4574.0, tp=4512.0, exits=[
        {"name": "bounce_reclaim_invalidation",
         "conditions": [{"type": "price_above", "level": 4580.0}]}
    ])
    results.append(run_test(
        "3. test_sell_invalidation_above_sl_rejected",
        plan, expect_reject=True,
        must_contain=["4580", "4574", "AT OR ABOVE", "SELL"],
    ))

    # 4. SELL invalidation BELOW SL — ACCEPT
    plan = make_plan("SELL", sl=4574.0, tp=4512.0, exits=[
        {"name": "thesis_invalidation",
         "conditions": [{"type": "price_above", "level": 4570.0}]}
    ])
    results.append(run_test(
        "4. test_sell_invalidation_below_sl_accepted",
        plan, expect_reject=False,
    ))

    # 5. BUY take-profit-side trigger — ACCEPT (no constraint on TP-side)
    plan = make_plan("BUY", sl=4543.0, tp=4605.0, exits=[
        {"name": "take_profit_at_resistance",
         "conditions": [{"type": "price_above", "level": 4605.0}]}
    ])
    results.append(run_test(
        "5. test_buy_take_profit_side_no_constraint",
        plan, expect_reject=False,
    ))

    # 6. Compound exit — only price-leg checked
    plan = make_plan("BUY", sl=4543.0, tp=4605.0, exits=[
        {"name": "compound_exit",
         "conditions": [
             {"type": "profit_pips", "op": "above", "threshold": 200.0},
             {"type": "rsi", "tf": "H1", "op": "above", "threshold": 65.0},
             {"type": "price_below", "level": 4525.0},
         ]}
    ])
    results.append(run_test(
        "6. test_compound_exit_only_price_legs_checked",
        plan, expect_reject=True,
        must_contain=["4525", "4543"],
    ))

    print("=" * 80)
    print("PART 2 — Real plan replay (last 10 closed plans through the new check)")
    print("=" * 80 + "\n")

    con = sqlite3.connect("data/history.db")
    con.row_factory = sqlite3.Row
    rows = con.execute("""
        SELECT id, plan_json FROM snow_plans WHERE status='closed'
        ORDER BY closed_at DESC LIMIT 10
    """).fetchall()

    expected_reject = {"PLAN-20260504-009", "PLAN-20260504-010",
                       "PLAN-20260504-012", "PLAN-20260504-002",
                       "PLAN-20260504-006", "PLAN-20260501-031"}

    for r in rows:
        p = json.loads(r["plan_json"])
        # Reduce to the shape the new check expects
        slim = {"entry": p["entry"], "exit": p.get("exit", [])}
        errs = _check_exit_geometry_vs_sl(slim)
        marker = "REJECT" if errs else "PASS"
        expected = "REJECT" if r["id"] in expected_reject else "PASS"
        match = "ok" if marker == expected else "MISMATCH"
        print(f"  [{match}] {r['id']}  validator: {marker}  expected: {expected}")
        for e in errs:
            print(f"      {e[:300]}")
    con.close()

    print()
    print("=" * 80)
    print(f"Summary: {sum(results)}/{len(results)} synthetic tests passed")
    print("=" * 80)


if __name__ == "__main__":
    main()
