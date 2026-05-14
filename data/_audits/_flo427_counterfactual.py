"""FLO-427 — counterfactual proxy against May 1-4 wrong-direction losses.

LIMITATION: The full counterfactual would replay
`regime_detector.detect_market_regime()` against MT5 H4/H1 candles at each
losing plan's `created_at`. That requires reconstructing 6 inputs
(tech_data, momentum_data, vol_status, brain_result, current_price,
atr_history) which were not persisted historically. `regime_state.json`
history only covers ~36h (May 13-14).

This script uses a PROXY: Floki's self-reported regime + direction
language from `thesis_oneline` in `data/_audits/claude_era_plans_may1_4.tsv`.
If Floki's thesis said "Countertrend" or named a regime opposite to the
trade direction, FLO-427 would have flagged the same plan — assuming the
ADX/confidence floors aligned with Floki's read.

This is a directional indicator, not authoritative. The full replay is a
follow-up workstream (FLO-427 P1: capture regime snapshots at trigger
time so May+ losses get audited with authoritative data).
"""

from __future__ import annotations

import csv
import re
import sys
from pathlib import Path

TSV = Path(__file__).parent / "claude_era_plans_may1_4.tsv"


def classify_thesis_regime(thesis: str) -> str:
    """Return inferred regime from Floki's prose. One of:
    TRENDING_BULLISH, TRENDING_BEARISH, RANGING, UNCLEAR.
    """
    t = thesis.lower()
    # Explicit regime tags first
    if "bullish regime" in t or "bullish trend" in t:
        return "TRENDING_BULLISH"
    if "bearish regime" in t or "bearish trend" in t or "bearish impulse" in t or "bearish momentum" in t:
        return "TRENDING_BEARISH"
    if "ranging regime" in t or "range-bound" in t or "within ranging" in t:
        return "RANGING"
    # Counter-trend marker = Floki acknowledged the trend was opposite
    if "countertrend" in t or "counter-trend" in t or "counter trend" in t:
        if "h4 support" in t or "deeper bounce" in t or "buy" in t.split("—")[0]:
            return "TRENDING_BEARISH"  # he's buying against bearish trend
        return "TRENDING_BULLISH"  # he's selling against bullish trend
    if "continuation" in t and ("bearish" in t or "breakdown" in t):
        return "TRENDING_BEARISH"
    if "continuation" in t and ("bullish" in t or "breakout" in t):
        return "TRENDING_BULLISH"
    if "mean reversion" in t or "fade" in t:
        return "RANGING"
    return "UNCLEAR"


def would_block(regime: str, direction: str) -> bool:
    """Apply the FLO-427 gate. Assumes high confidence + ADX ≥ 25 — i.e.,
    if the regime was strong enough that Floki himself named it, the gate
    would have caught the counter-direction trade."""
    if regime == "TRENDING_BULLISH" and direction == "SELL":
        return True
    if regime == "TRENDING_BEARISH" and direction == "BUY":
        return True
    return False


def main():
    if not TSV.exists():
        print(f"ERROR: TSV not found at {TSV}", file=sys.stderr)
        return 1

    with TSV.open() as f:
        rows = list(csv.DictReader(f, delimiter="\t"))

    # All plans that fired AND closed with negative PnL (losing trades).
    losers = []
    for r in rows:
        if not r.get("plan_id"):
            continue
        if r.get("fired") != "Y":
            continue
        try:
            usd = float(r.get("usd") or 0)
        except ValueError:
            continue
        if usd >= 0:
            continue
        losers.append(r)

    print(f"=== FLO-427 counterfactual (proxy: thesis_oneline) ===")
    print(f"Source: {TSV}")
    print(f"Losing fired plans across May 1-4: {len(losers)}\n")

    print(f"{'plan_id':<25} {'dir':<5} {'usd':>8} {'verdict':<22} {'inferred_regime':<20} {'BLOCK?':<6}")
    print("-" * 95)

    blocked = 0
    wrong_dir = 0
    for r in losers:
        thesis = r.get("thesis_oneline", "")
        direction = r.get("direction", "")
        regime = classify_thesis_regime(thesis)
        block = would_block(regime, direction)
        verdict = r.get("verdict", "")
        is_wrong_dir = verdict == "WRONG_FULL_REVERSE"
        if is_wrong_dir:
            wrong_dir += 1
        if block:
            blocked += 1
        marker = "YES" if block else "no"
        print(f"{r['plan_id']:<25} {direction:<5} {float(r['usd']):>8.2f} {verdict:<22} {regime:<20} {marker:<6}")

    print()
    print(f"Total losers:           {len(losers)}")
    print(f"WRONG_FULL_REVERSE:     {wrong_dir}")
    print(f"Would-block (proxy):    {blocked}/{len(losers)}")
    if wrong_dir > 0:
        print(f"Would-block / wrong-dir: {blocked}/{wrong_dir} = {blocked*100//max(wrong_dir,1)}%")

    print()
    print("DECISION GATE:")
    if blocked >= 3:
        print(f"  blocked={blocked} >= 3  -> SHIP FLO-427 unchanged.")
        return 0
    elif blocked >= 1:
        print(f"  blocked={blocked} in [1,2] -> SHIP but expect modest impact. "
              "Track REGIME_GATE_REJECT log lines for two cycles before tuning.")
        return 0
    else:
        print(f"  blocked={blocked} == 0 -> ABORT. The proxy detected no wrong-")
        print("  direction trades the gate would catch. Either thesis text is")
        print("  too ambiguous (proxy limitation) OR regime_detector wouldn't")
        print("  have flagged either. Escalate before shipping.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
