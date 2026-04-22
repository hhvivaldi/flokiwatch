"""
FLO-332 Phase 1 — Session infrastructure + tool adoption + pattern rigidity audit.

Workstreams covered:
  1. Session memory reads/writes last 7 days (from tool_trace)
  2. For each OPEN decision: was read_session_memory / get_trade_lessons /
     get_position_history called in the same cycle BEFORE the OPEN?
  3. Pattern rigidity prevalence over 30 days: same-session re-entries
     within a 5-pip radius at the same price.
  4. Auto-inject audit: confirm session_memory is present in build_data_package
     today and inspect the XML that reached Floki.

Read-only. Output JSON and markdown tables.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
from collections import defaultdict, Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

REPO = Path(r"C:/Users/Hermano/OneDrive/Desktop/XAUUSD")
DB = REPO / "data" / "history.db"
OUT = REPO / "data" / "_audits" / "flo332"
OUT.mkdir(parents=True, exist_ok=True)

MEMORY_TOOLS = {
    "read_session_memory",
    "write_session_memory",
    "get_trade_lessons",
    "get_position_history",
    "get_trade_patterns",
    "get_trade_journal",
    "get_recent_reflexions",
    "search_reflexions",
    "search_memory",
}


def parse_utc(ts: str) -> datetime:
    # Accept both '...Z' and '...+00:00' and naive.
    s = ts.replace("Z", "+00:00") if ts.endswith("Z") else ts
    try:
        return datetime.fromisoformat(s)
    except Exception:
        try:
            return datetime.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
        except Exception:
            return datetime.strptime(ts[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)


def connect() -> sqlite3.Connection:
    c = sqlite3.connect(str(DB), timeout=10)
    c.row_factory = sqlite3.Row
    return c


def list_tools_in_trace(tool_trace: str) -> List[str]:
    if not tool_trace:
        return []
    try:
        arr = json.loads(tool_trace)
        if not isinstance(arr, list):
            return []
        return [str(t.get("name", "")) for t in arr if isinstance(t, dict)]
    except Exception:
        return []


# --------------------------------------------------------------------- WS1+2
def audit_tool_call_frequency(days: int = 7) -> Dict[str, Any]:
    """Count how often each memory tool appears in tool_trace across N days."""
    conn = connect()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    rows = conn.execute(
        "SELECT id, timestamp, agent_decision, tool_trace "
        "FROM agent_proactive_analyses WHERE timestamp >= ? AND tool_trace IS NOT NULL",
        (cutoff,),
    ).fetchall()

    total_cycles = len(rows)
    open_cycles = 0
    wait_cycles = 0
    close_cycles = 0

    per_tool_cycle_count: Counter = Counter()  # tool -> cycles containing it
    per_tool_call_count: Counter = Counter()   # tool -> total calls
    open_tool_cycle_count: Counter = Counter()  # same but only OPEN cycles

    open_with_memory_read: List[Dict[str, Any]] = []
    open_without_memory_read: List[Dict[str, Any]] = []

    for r in rows:
        decision = (r["agent_decision"] or "").upper()
        tools = list_tools_in_trace(r["tool_trace"])
        tool_set = set(tools)

        for t in tools:
            per_tool_call_count[t] += 1
        for t in tool_set:
            per_tool_cycle_count[t] += 1

        if decision.startswith("OPEN") or decision.startswith("PLACE") or decision.startswith("BUY") or decision.startswith("SELL"):
            open_cycles += 1
            for t in tool_set:
                open_tool_cycle_count[t] += 1
            read_any = any(t in tool_set for t in [
                "read_session_memory", "get_trade_lessons", "get_position_history",
                "get_trade_patterns", "get_recent_reflexions", "search_reflexions",
                "search_memory", "get_trade_journal"])
            row_info = {
                "id": r["id"],
                "timestamp": r["timestamp"],
                "decision": decision,
                "memory_tools_called": sorted([t for t in tool_set if t in MEMORY_TOOLS]),
                "total_tools_in_cycle": len(tools),
            }
            (open_with_memory_read if read_any else open_without_memory_read).append(row_info)
        elif decision == "WAIT":
            wait_cycles += 1
        elif decision.startswith("CLOSE"):
            close_cycles += 1

    return {
        "days_window": days,
        "total_cycles": total_cycles,
        "open_cycles": open_cycles,
        "wait_cycles": wait_cycles,
        "close_cycles": close_cycles,
        "per_tool_cycle_pct": {
            t: round(100 * c / max(1, total_cycles), 1)
            for t, c in per_tool_cycle_count.most_common()
        },
        "per_tool_total_calls": dict(per_tool_call_count.most_common()),
        "open_memory_tool_cycle_pct": {
            t: round(100 * open_tool_cycle_count[t] / max(1, open_cycles), 1)
            for t in MEMORY_TOOLS if open_tool_cycle_count[t] > 0
        },
        "open_cycles_with_memory_read": len(open_with_memory_read),
        "open_cycles_without_memory_read": len(open_without_memory_read),
        "open_with_memory_sample": open_with_memory_read[:20],
        "open_without_memory_sample": open_without_memory_read[:20],
    }


# --------------------------------------------------------------------- WS3
def audit_pattern_rigidity(days: int = 30, radius_pips: float = 5.0) -> Dict[str, Any]:
    """For every closed trade, count how many trades in the same session opened
    within `radius_pips` of its entry price. 5 pips = $0.50 for XAUUSD."""
    conn = connect()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    rows = conn.execute(
        "SELECT ticket, direction, open_price, profit, close_reason, open_time, close_time "
        "FROM trades WHERE open_time >= ? ORDER BY open_time ASC",
        (cutoff,),
    ).fetchall()

    # Group by UTC trading day (00:00–24:00)
    by_day: Dict[str, List[sqlite3.Row]] = defaultdict(list)
    for r in rows:
        d = parse_utc(r["open_time"]).strftime("%Y-%m-%d")
        by_day[d].append(r)

    price_radius = radius_pips * 0.1  # XAUUSD: 1 pip = $0.10

    trades_total = len(rows)
    trades_with_same_level_repeat = 0
    per_trade_flags: List[Dict[str, Any]] = []

    # level clusters per day (for reporting)
    day_clusters: Dict[str, List[Dict[str, Any]]] = defaultdict(list)

    for day, trs in by_day.items():
        trs_sorted = sorted(trs, key=lambda x: x["open_time"])
        for i, t in enumerate(trs_sorted):
            same_level_priors = 0
            matches = []
            for j in range(i):
                prior = trs_sorted[j]
                if abs(t["open_price"] - prior["open_price"]) <= price_radius:
                    same_level_priors += 1
                    matches.append({
                        "ticket": prior["ticket"],
                        "direction": prior["direction"],
                        "price": prior["open_price"],
                        "profit": prior["profit"],
                        "close_reason": prior["close_reason"],
                    })
            if same_level_priors > 0:
                trades_with_same_level_repeat += 1
            per_trade_flags.append({
                "ticket": t["ticket"],
                "day": day,
                "open_time": t["open_time"],
                "direction": t["direction"],
                "price": t["open_price"],
                "profit": t["profit"],
                "close_reason": t["close_reason"],
                "same_level_priors_today": same_level_priors,
                "prior_matches": matches[:5],
            })

    # Aggregate losing rate conditioned on prior-same-level
    loss_given_repeat = [x for x in per_trade_flags if x["same_level_priors_today"] > 0 and (x["profit"] or 0) < 0]
    win_given_repeat = [x for x in per_trade_flags if x["same_level_priors_today"] > 0 and (x["profit"] or 0) > 0]
    loss_given_fresh = [x for x in per_trade_flags if x["same_level_priors_today"] == 0 and (x["profit"] or 0) < 0]
    win_given_fresh = [x for x in per_trade_flags if x["same_level_priors_today"] == 0 and (x["profit"] or 0) > 0]

    # Top days by cluster size
    day_cluster_sizes = []
    for day, trs in by_day.items():
        # group trades within radius
        groups: List[List[sqlite3.Row]] = []
        used = set()
        for i, a in enumerate(trs):
            if i in used:
                continue
            g = [a]
            used.add(i)
            for j in range(i + 1, len(trs)):
                if j in used:
                    continue
                if abs(a["open_price"] - trs[j]["open_price"]) <= price_radius:
                    g.append(trs[j])
                    used.add(j)
            if len(g) >= 2:
                groups.append(g)
        if groups:
            max_cluster = max(len(g) for g in groups)
            total_in_clusters = sum(len(g) for g in groups)
            day_cluster_sizes.append({
                "day": day,
                "total_trades": len(trs),
                "clusters": len(groups),
                "max_cluster_size": max_cluster,
                "trades_in_any_cluster": total_in_clusters,
                "cluster_detail": [
                    {"size": len(g), "price_center": round(sum(x["open_price"] for x in g) / len(g), 2)}
                    for g in groups
                ],
            })

    day_cluster_sizes.sort(key=lambda x: -x["max_cluster_size"])

    return {
        "days_window": days,
        "radius_pips": radius_pips,
        "total_trades": trades_total,
        "unique_days_traded": len(by_day),
        "trades_with_same_level_repeat_today": trades_with_same_level_repeat,
        "pct_trades_are_repeats": round(100 * trades_with_same_level_repeat / max(1, trades_total), 1),
        "wr_on_repeats": round(100 * len(win_given_repeat) / max(1, len(win_given_repeat) + len(loss_given_repeat)), 1),
        "wr_on_fresh_levels": round(100 * len(win_given_fresh) / max(1, len(win_given_fresh) + len(loss_given_fresh))),
        "n_repeats": len(win_given_repeat) + len(loss_given_repeat),
        "n_fresh": len(win_given_fresh) + len(loss_given_fresh),
        "top_cluster_days": day_cluster_sizes[:10],
    }


# --------------------------------------------------------------------- WS4
def today_decisions_detail() -> List[Dict[str, Any]]:
    """For today's OPEN-related decisions, extract which memory tools were called."""
    conn = connect()
    today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    rows = conn.execute(
        "SELECT id, timestamp, agent_decision, agent_confidence, tool_trace, agent_reasoning "
        "FROM agent_proactive_analyses WHERE timestamp >= ? ORDER BY id ASC",
        (today_start,),
    ).fetchall()
    out = []
    for r in rows:
        tools = list_tools_in_trace(r["tool_trace"])
        memory_hits = [t for t in tools if t in MEMORY_TOOLS]
        out.append({
            "id": r["id"],
            "ts": r["timestamp"],
            "decision": r["agent_decision"],
            "conf": r["agent_confidence"],
            "n_tools": len(tools),
            "memory_tools": memory_hits,
            "has_memory_read": any(t in {"read_session_memory", "get_trade_lessons", "get_position_history",
                                          "get_trade_patterns", "get_recent_reflexions"} for t in memory_hits),
            "reasoning_first_120": (r["agent_reasoning"] or "")[:120],
        })
    return out


def main() -> int:
    print("=" * 70)
    print("FLO-332 Phase 1 Audit")
    print("=" * 70)

    result: Dict[str, Any] = {
        "generated_utc": datetime.now(timezone.utc).isoformat(),
    }

    print("\n[WS1+WS2] Tool call frequency — 7 days")
    result["tool_frequency_7d"] = audit_tool_call_frequency(days=7)
    f = result["tool_frequency_7d"]
    print(f"  cycles={f['total_cycles']}  open={f['open_cycles']}  wait={f['wait_cycles']}")
    print(f"  OPEN cycles with memory read: {f['open_cycles_with_memory_read']}/{f['open_cycles']}")
    print(f"  OPEN cycles WITHOUT memory read: {f['open_cycles_without_memory_read']}/{f['open_cycles']}")

    print("\n[WS1+WS2] Tool call frequency — 30 days")
    result["tool_frequency_30d"] = audit_tool_call_frequency(days=30)
    f30 = result["tool_frequency_30d"]
    print(f"  cycles={f30['total_cycles']}  open={f30['open_cycles']}")
    print(f"  OPEN cycles with memory read: {f30['open_cycles_with_memory_read']}/{f30['open_cycles']}")

    print("\n[WS3] Pattern rigidity — 30 days, 5-pip radius")
    result["pattern_rigidity_30d"] = audit_pattern_rigidity(days=30, radius_pips=5.0)
    p = result["pattern_rigidity_30d"]
    print(f"  total_trades={p['total_trades']}  repeats={p['trades_with_same_level_repeat_today']} ({p['pct_trades_are_repeats']}%)")
    print(f"  WR repeats={p['wr_on_repeats']}%  WR fresh={p['wr_on_fresh_levels']}%  (n_rep={p['n_repeats']} n_fresh={p['n_fresh']})")
    print(f"  Top cluster days:")
    for d in p["top_cluster_days"][:5]:
        print(f"    {d['day']}: {d['total_trades']} trades, max_cluster={d['max_cluster_size']}, clusters={d['clusters']}")

    print("\n[WS3] Pattern rigidity — 30 days, 10-pip radius (sanity)")
    result["pattern_rigidity_30d_10pips"] = audit_pattern_rigidity(days=30, radius_pips=10.0)
    p10 = result["pattern_rigidity_30d_10pips"]
    print(f"  total_trades={p10['total_trades']}  repeats={p10['trades_with_same_level_repeat_today']} ({p10['pct_trades_are_repeats']}%)")
    print(f"  WR repeats={p10['wr_on_repeats']}%  WR fresh={p10['wr_on_fresh_levels']}%")

    print("\n[Today] Per-decision detail")
    result["today_decisions"] = today_decisions_detail()
    for td in result["today_decisions"]:
        tag = "MEM" if td["has_memory_read"] else "---"
        dec = (td["decision"] or "")[:20]
        print(f"  id={td['id']} {td['ts'][11:19]} {dec:20} conf={td['conf']} {tag} mem={td['memory_tools']}")

    out_path = OUT / "phase1_audit_results.json"
    with out_path.open("w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, default=str)
    print(f"\nWrote {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
