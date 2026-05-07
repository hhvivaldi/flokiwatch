"""[RESEARCH/REPRO ONLY — NOT IMPORTED BY PRODUCTION CODE]

FLO-425 PR-A historical backtest of the breakout lifecycle classifier.

For each closed plan in the last 60 days (lifecycle setup_types only):
  1. Load author + trigger regime snapshots from snow_plans (or None
     for plans authored pre-FLO-422 Step 3).
  2. Pull M5 candles around entered_at via MT5 (broker offset +3h).
  3. Run classify_breakout_lifecycle at:
       offset 0   = entered_at (trigger time)
       offset +30m, +60m, +90m
  4. Emit one CSV row per (plan_id, offset_min).
  5. Aggregate: per-phase WR + mean pips, score-vs-outcome correlation,
     today's BUY-cluster (PLAN-004/005/007) recall check.

Output:
  data/_audits/_breakout_lifecycle_backtest_rows.csv     — per-row detail
  data/_audits/_breakout_lifecycle_backtest_summary.txt  — aggregate stats

Answers (per CEO directive):
  - Do EXHAUSTION / FAILURE phases correlate with losers?
  - Do ACCEPTED / CONTINUATION phases correlate with winners?
  - Does acceptance_quality separate outcomes?
  - Does exhaustion_probability separate outcomes?
"""
from __future__ import annotations
import csv
import json
import os
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from breakout_lifecycle import classify_breakout_lifecycle
from mt5_safe import mt5, mt5_lock

BROKER_OFFSET_HOURS = 3
LOOKBACK_DAYS = 60
LIFECYCLE_SETUPS = (
    "breakout_range",
    "continuation_momentum",
    "pullback_trend",
    "structural_bounce",
)
OFFSETS_MIN = (0, 30, 60, 90)

DB_PATH = "data/history.db"
ROWS_OUT = "data/_audits/_breakout_lifecycle_backtest_rows.csv"
SUMMARY_OUT = "data/_audits/_breakout_lifecycle_backtest_summary.txt"


def _load_closed_plans() -> List[Dict[str, Any]]:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)).isoformat()[:19]
    conn = sqlite3.connect(DB_PATH)
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT id, plan_json, status, trade_ticket, entered_at, closed_at,
                   outcome_pips, author_regime_snapshot_json,
                   trigger_regime_snapshot_json
              FROM snow_plans
             WHERE status = 'closed'
               AND entered_at IS NOT NULL
               AND entered_at >= ?
             ORDER BY entered_at
        """, (cutoff,))
        rows = cur.fetchall()
    finally:
        conn.close()

    out = []
    for r in rows:
        try:
            pj = json.loads(r[1])
            setup_type = pj.get("analysis", {}).get("setup_type")
            if setup_type not in LIFECYCLE_SETUPS:
                continue
            direction = pj.get("entry", {}).get("direction")
            entry_price = pj.get("entry", {}).get("entry_price")
            author_snap = json.loads(r[7]) if r[7] else None
            trigger_snap = json.loads(r[8]) if r[8] else None
            out.append({
                "id": r[0],
                "plan_dict": pj,
                "setup_type": setup_type,
                "direction": direction,
                "entry_price": entry_price,
                "entered_at": r[4],
                "closed_at": r[5],
                "outcome_pips": r[6],
                "author_snapshot": author_snap,
                "trigger_snapshot": trigger_snap,
            })
        except Exception as e:
            print(f"  skip {r[0]}: {type(e).__name__}: {e}")
    return out


def _fetch_m5_around(ts_iso: str, minutes_after: int = 0,
                    n_bars: int = 30) -> List[Dict[str, Any]]:
    """Pull `n_bars` M5 candles ending at (eval_ts = ts_iso + minutes_after),
    INCLUSIVE of the bar that contains eval_ts.

    PR-A1 fix. Two issues stacked:
      (a) copy_rates_from(end, count) returned bars STRICTLY BEFORE the
          given timestamp — excluded the entry bar at offset=0, which
          made PLAN-004 / PLAN-007 backtest as BUILDUP.
      (b) MT5's `end` parameter is interpreted with an empirical +2h
          shift beyond BROKER_OFFSET_HOURS (likely a server-DST quirk
          on this broker). Passing a "naive broker wallclock" derived
          purely from BROKER_OFFSET_HOURS=3 returned bars 2h earlier
          than expected.

    Robust strategy: oversize the request cushion (5h beyond eval_ts),
    then TRIM the returned bars to those whose timestamp <= the bar
    that contains eval_ts. Trim is by the bar's epoch directly, which
    the broker stamps consistently as "broker_wallclock_as_utc".
    """
    if not ts_iso:
        return []
    try:
        ts_utc = datetime.fromisoformat(ts_iso.replace("Z", "+00:00"))
    except Exception:
        return []
    eval_utc = ts_utc + timedelta(minutes=minutes_after)

    # The bar that "contains" eval_utc is the M5 bar whose 5-min open
    # is <= eval_utc. The bar's epoch decodes-as-UTC to broker wallclock,
    # which is real_utc + BROKER_OFFSET_HOURS.
    keep_threshold_broker_naive = (
        eval_utc + timedelta(hours=BROKER_OFFSET_HOURS)
    ).replace(tzinfo=None)

    # Oversized request cushion: +5h beyond eval (bigger than any
    # plausible broker-DST shift). Empirically MT5 lags the requested
    # end by ~2h on this server; +5h covers it with margin.
    end_request = (
        eval_utc + timedelta(hours=BROKER_OFFSET_HOURS + 5, minutes=5)
    ).replace(tzinfo=None)

    with mt5_lock:
        if not mt5.initialize():
            return []
        mt5.symbol_select("XAUUSD", True)
        rates = mt5.copy_rates_from(
            "XAUUSD", mt5.TIMEFRAME_M5, end_request, n_bars + 60,
        )
    if rates is None or len(rates) == 0:
        return []

    # Filter: keep bars whose stored time (decode_as_utc = broker
    # wallclock) is at or before the bar containing eval_utc.
    threshold_unix = keep_threshold_broker_naive.replace(
        tzinfo=timezone.utc,
    ).timestamp()

    kept = [r for r in rates if int(r["time"]) <= int(threshold_unix)]
    if not kept:
        return []

    bars = []
    for r in kept:
        bars.append({
            "open": float(r["open"]),
            "high": float(r["high"]),
            "low": float(r["low"]),
            "close": float(r["close"]),
        })
    return bars[-n_bars:]


def _classify_at_offsets(plan_row: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Run classifier at multiple offsets after entered_at. One row per offset."""
    out = []
    entered_iso = plan_row["entered_at"]
    try:
        entered_utc = datetime.fromisoformat(entered_iso.replace("Z", "+00:00"))
    except Exception:
        return out

    for offset_min in OFFSETS_MIN:
        candles = _fetch_m5_around(entered_iso, minutes_after=offset_min, n_bars=30)
        eval_ts = entered_utc + timedelta(minutes=offset_min)
        result = classify_breakout_lifecycle(
            plan_dict=plan_row["plan_dict"],
            author_snapshot=plan_row["author_snapshot"],
            trigger_snapshot=plan_row["trigger_snapshot"],
            candles_m5=candles,
            eval_ts=eval_ts,
        )
        out.append({
            "plan_id": plan_row["id"],
            "setup_type": plan_row["setup_type"],
            "direction": plan_row["direction"],
            "entered_at": entered_iso,
            "closed_at": plan_row["closed_at"],
            "outcome_pips": plan_row["outcome_pips"],
            "win": 1 if (plan_row["outcome_pips"] or 0) > 0 else 0,
            "offset_min": offset_min,
            "phase": result.get("phase"),
            "phase_confidence": result.get("phase_confidence"),
            "freshness": result.get("breakout_freshness"),
            "maturity": result.get("breakout_maturity"),
            "acceptance_quality": result.get("acceptance_quality"),
            "exhaustion_probability": result.get("exhaustion_probability"),
            "n_candles_pulled": len(candles),
            "n_warnings": len(result.get("warnings") or []),
            "reasons_first": (result.get("reasons") or [None])[0],
        })
    return out


def _aggregate(rows: List[Dict[str, Any]]) -> str:
    """Build the summary text. Focus on the 4 questions in the docstring."""
    lines: List[str] = []
    lines.append("=" * 80)
    lines.append("FLO-425 PR-A LIFECYCLE BACKTEST SUMMARY")
    lines.append("=" * 80)
    lines.append(f"total rows: {len(rows)}")
    n_plans = len({r["plan_id"] for r in rows})
    lines.append(f"unique plans: {n_plans}")
    lines.append("")

    # Per-phase WR + mean pips, restricted to offset_min == 0 (trigger time)
    trigger_rows = [r for r in rows if r["offset_min"] == 0]
    by_phase = defaultdict(list)
    for r in trigger_rows:
        if r["phase"] is None:
            continue
        by_phase[r["phase"]].append(r)
    lines.append("Per-phase outcome AT TRIGGER TIME (offset=0):")
    lines.append(f"  {'phase':<20} {'n':>4} {'WR%':>6} {'mean_pips':>11} {'net_pips':>10}")
    for phase in ("BUILDUP", "BREAK_ATTEMPT", "ACCEPTANCE_TEST", "ACCEPTED",
                  "CONTINUATION", "EXHAUSTION", "FAILURE", "INSUFFICIENT_DATA"):
        plans = by_phase.get(phase, [])
        if not plans:
            continue
        n = len(plans)
        wins = sum(p["win"] for p in plans)
        wr = 100.0 * wins / n
        net = sum(p["outcome_pips"] or 0 for p in plans)
        mean = net / n
        lines.append(f"  {phase:<20} {n:>4} {wr:>6.1f} {mean:>11.1f} {net:>10.1f}")
    lines.append("")

    # Per-phase outcome at +30m (does the phase EVOLVE in a way that
    # discriminates? CONTINUATION at +30m is more informative than at trigger)
    plus30 = [r for r in rows if r["offset_min"] == 30]
    by_phase_30 = defaultdict(list)
    for r in plus30:
        if r["phase"] is None:
            continue
        by_phase_30[r["phase"]].append(r)
    lines.append("Per-phase outcome at +30m (post-trigger evolution):")
    lines.append(f"  {'phase':<20} {'n':>4} {'WR%':>6} {'mean_pips':>11} {'net_pips':>10}")
    for phase in ("BUILDUP", "BREAK_ATTEMPT", "ACCEPTANCE_TEST", "ACCEPTED",
                  "CONTINUATION", "EXHAUSTION", "FAILURE", "INSUFFICIENT_DATA"):
        plans = by_phase_30.get(phase, [])
        if not plans:
            continue
        n = len(plans)
        wins = sum(p["win"] for p in plans)
        wr = 100.0 * wins / n
        net = sum(p["outcome_pips"] or 0 for p in plans)
        mean = net / n
        lines.append(f"  {phase:<20} {n:>4} {wr:>6.1f} {mean:>11.1f} {net:>10.1f}")
    lines.append("")

    # Score-vs-outcome separation: bucket scores into low/mid/high, compare WR
    def _bucket(v: Optional[float]) -> Optional[str]:
        if v is None:
            return None
        if v < 0.33:
            return "low"
        if v < 0.67:
            return "mid"
        return "high"

    for score_name in ("acceptance_quality", "exhaustion_probability",
                       "freshness", "maturity"):
        lines.append(f"{score_name} buckets at trigger (offset=0):")
        lines.append(f"  {'bucket':<8} {'n':>4} {'WR%':>6} {'mean_pips':>11}")
        bucketed = defaultdict(list)
        for r in trigger_rows:
            b = _bucket(r.get(score_name))
            if b:
                bucketed[b].append(r)
        for b in ("low", "mid", "high"):
            plans = bucketed.get(b, [])
            if not plans:
                continue
            n = len(plans)
            wins = sum(p["win"] for p in plans)
            wr = 100.0 * wins / n
            net = sum(p["outcome_pips"] or 0 for p in plans)
            mean = net / n
            lines.append(f"  {b:<8} {n:>4} {wr:>6.1f} {mean:>11.1f}")
        lines.append("")

    # Today's BUY-cluster recall check
    lines.append("Today's BUY-cluster recall (PLAN-004 / PLAN-005 / PLAN-007):")
    for pid in ("PLAN-20260507-004", "PLAN-20260507-005", "PLAN-20260507-007"):
        these = [r for r in rows if r["plan_id"] == pid]
        if not these:
            lines.append(f"  {pid}: NOT FOUND in dataset")
            continue
        for r in sorted(these, key=lambda x: x["offset_min"]):
            lines.append(
                f"  {pid:<22} +{r['offset_min']:>2}m  phase={r['phase']:<18} "
                f"acc_q={r['acceptance_quality']} "
                f"exh={r['exhaustion_probability']}  outcome={r['outcome_pips']}p"
            )
    lines.append("")

    # Question-by-question pass/fail check (qualitative — doesn't gate
    # anything, just reports)
    lines.append("CEO question recall:")
    lines.append("Q1 do EXHAUSTION/FAILURE correlate with losers?")
    for phase in ("EXHAUSTION", "FAILURE"):
        plans = by_phase.get(phase, [])
        if plans:
            wr = 100.0 * sum(p["win"] for p in plans) / len(plans)
            lines.append(f"   {phase}: WR={wr:.1f}% (lower-is-confirming)")
    lines.append("Q2 do ACCEPTED/CONTINUATION correlate with winners?")
    for phase in ("ACCEPTED", "CONTINUATION"):
        plans = by_phase.get(phase, [])
        if plans:
            wr = 100.0 * sum(p["win"] for p in plans) / len(plans)
            lines.append(f"   {phase}: WR={wr:.1f}% (higher-is-confirming)")
    lines.append("Q3+Q4 separation visible in score buckets above.")
    lines.append("=" * 80)
    return "\n".join(lines)


def main() -> int:
    print("Loading closed plans...")
    plans = _load_closed_plans()
    print(f"  {len(plans)} lifecycle-eligible closed plans found.")

    print("Classifying at offsets 0/30/60/90 min...")
    all_rows: List[Dict[str, Any]] = []
    for i, p in enumerate(plans, 1):
        rows = _classify_at_offsets(p)
        all_rows.extend(rows)
        if i % 10 == 0:
            print(f"  {i}/{len(plans)} processed")

    print(f"Writing {len(all_rows)} rows to {ROWS_OUT}")
    if all_rows:
        with open(ROWS_OUT, "w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(all_rows[0].keys()))
            writer.writeheader()
            writer.writerows(all_rows)

    summary = _aggregate(all_rows)
    with open(SUMMARY_OUT, "w", encoding="utf-8") as f:
        f.write(summary + "\n")
    print(summary)
    print(f"\nSummary written to {SUMMARY_OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
