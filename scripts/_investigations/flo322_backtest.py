"""
FLO-322 Phase 3 — H1-b counterfactual backtest.

Tests: under a new prompt rule of thumb `SL distance >= spread + 1.0 * weighted_ATR`,
with weighted_ATR = w*M5_ATR + (1-w)*H1_ATR, would Floki's historical trades have
fared better?

Data sources:
  - data/history.db trades table (MFE/MAE, SL, profit, close_reason, close_time, volume)
  - data/trade_conditions/*.json (atr_h1 at open — WAS visible to Floki)
  - MT5 live history_deals_get + copy_rates_from (M5 ATR reconstruction at open)

Critical assumptions (disclosed in report):
  - Spread at open = 3.0 points (fixed, typical XAU normal-session)
  - M5_ATR = Wilder ATR(14) on M5 candles ending 5min BEFORE open_time (approximation of
    what Floki would have seen if M5_ATR were pre-injected)
  - Broker time = UTC+3 (see FLO-328 Phase 1 — deal.time is broker-as-epoch)
  - $/point scaling per trade: derived from actual trade (abs(profit) / sl_points for SL
    closes; from volume field for others assuming $1/pt/0.01lot convention)
  - Counterfactual outcome models:
      Model A "Obedience": SL widened to floor exactly
      Model B "Partial-50": SL = 0.5 × (actual + floor)
  - For trades whose new SL would have prevented the stopout (MAE < new_SL_dist):
    assume cf_profit = 0.5 × MFE × $/point (mid-MFE exit approximation)

Usage: python scripts/_investigations/flo322_backtest.py
Output: data/_audits/flo322/FLO-322_backtest_results.json
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from dataclasses import dataclass, asdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Optional

import numpy as np

try:
    import MetaTrader5 as mt5
except Exception:
    print("MetaTrader5 not available — backtest cannot run", file=sys.stderr)
    sys.exit(2)


REPO = Path(__file__).resolve().parents[2]
TC_DIR = REPO / "data" / "trade_conditions"
HISTORY_DB = REPO / "data" / "history.db"
OUT_PATH = REPO / "data" / "_audits" / "flo322" / "FLO-322_backtest_results.json"

SPREAD_POINTS = 3.0           # assumed fixed XAU normal-session spread
BROKER_OFFSET_HOURS = 3       # MT5 broker is UTC+3 (verified FLO-328 Phase 1)
ATR_PERIOD = 14               # Wilder ATR


@dataclass
class Trade:
    ticket: int
    direction: str              # BUY or SELL
    open_time_utc: datetime     # actual UTC
    open_price: float
    sl: float                    # ORIGINAL SL at open
    final_sl: Optional[float]    # SL at close (after BE/trailing)
    close_price: Optional[float]
    profit: Optional[float]
    close_reason: str           # Stop Loss / Expert Advisor / Take Profit
    close_time: Optional[str]
    volume: float
    mfe_points: Optional[float]
    mae_points: Optional[float]
    atr_h1: float
    atr_m5: Optional[float]     # reconstructed
    session: Optional[str]

    @property
    def sl_dist_points(self) -> float:
        return abs(self.open_price - self.sl)

    @property
    def dollars_per_point(self) -> float:
        """$ per price-point for this trade's lot size."""
        return float(self.volume) * 100.0

    @property
    def hit_original_sl(self) -> bool:
        """True iff the trade's close was AT (or near) the original SL value,
        meaning BE/trailing did NOT intervene. Only these trades are candidates
        for H1-b to have any effect, because H1-b widens only the ORIGINAL SL
        at open — trailing SL firing earlier bypasses the widening entirely.
        """
        if self.close_reason.strip().lower() != "stop loss":
            return False
        if self.final_sl is None:
            return False
        return abs(self.sl - self.final_sl) < 0.5  # points — tolerate small slippage


def load_trades() -> list[Trade]:
    """Join history.db trades with trade_conditions/*.json on ticket."""
    conn = sqlite3.connect(str(HISTORY_DB))
    c = conn.cursor()
    rows = c.execute("""
        SELECT ticket, direction, volume, open_price, close_price, sl, final_sl, profit,
               close_reason, open_time, close_time, mfe_points, mae_points
        FROM trades
        WHERE sl > 0 AND close_time IS NOT NULL
          AND mfe_points IS NOT NULL AND mae_points IS NOT NULL
    """).fetchall()
    conn.close()

    trades: list[Trade] = []
    for r in rows:
        (ticket, direction, volume, open_price, close_price, sl, final_sl, profit,
         close_reason, open_time, close_time, mfe, mae) = r

        tc_path = TC_DIR / f"{ticket}.json"
        if not tc_path.exists():
            continue
        with tc_path.open() as f:
            tc = json.load(f)
        coa = tc.get("conditions_at_open") or {}
        atr_h1 = coa.get("atr_h1")
        if atr_h1 is None or atr_h1 <= 0:
            continue

        # Parse open_time. History.db stores UTC (naive or with Z).
        ot_str = open_time.replace("Z", "+00:00") if "Z" in open_time else open_time
        try:
            ot = datetime.fromisoformat(ot_str)
            if ot.tzinfo is None:
                ot = ot.replace(tzinfo=timezone.utc)
        except ValueError:
            continue

        trades.append(Trade(
            ticket=ticket,
            direction=(direction or "").upper(),
            open_time_utc=ot,
            open_price=float(open_price),
            sl=float(sl),
            final_sl=float(final_sl) if final_sl is not None else None,
            close_price=float(close_price) if close_price else None,
            profit=float(profit) if profit is not None else None,
            close_reason=close_reason or "",
            close_time=close_time,
            volume=float(volume),
            mfe_points=float(mfe) if mfe is not None else None,
            mae_points=float(mae) if mae is not None else None,
            atr_h1=float(atr_h1),
            atr_m5=None,
            session=coa.get("session"),
        ))
    return trades


def reconstruct_m5_atr(trades: list[Trade]) -> None:
    """Mutate: populate atr_m5 on each trade via MT5 copy_rates_from."""
    if not mt5.initialize():
        raise RuntimeError(f"mt5.initialize failed: {mt5.last_error()}")
    try:
        for t in trades:
            # Convert actual UTC open_time to broker-as-epoch view: MT5 interprets
            # naive datetime arguments as broker-local time. Our UTC datetime must
            # be shifted +3h so MT5 reads it as "same wall-clock instant in broker TZ".
            broker_ts = (t.open_time_utc + timedelta(hours=BROKER_OFFSET_HOURS)).replace(tzinfo=None)
            # Fetch 30 M5 candles ENDING at open_time (so we use what was visible then)
            rates = mt5.copy_rates_from("XAUUSD", mt5.TIMEFRAME_M5, broker_ts, 30)
            if rates is None or len(rates) < ATR_PERIOD + 1:
                continue
            high = rates["high"]
            low = rates["low"]
            close = rates["close"]
            tr = np.maximum(
                high[1:] - low[1:],
                np.maximum(np.abs(high[1:] - close[:-1]), np.abs(low[1:] - close[:-1])),
            )
            # Wilder ATR: simple mean of last ATR_PERIOD true ranges is close enough
            atr = float(tr[-ATR_PERIOD:].mean())
            t.atr_m5 = atr
    finally:
        mt5.shutdown()


def floor_points(trade: Trade, w_m5: float) -> Optional[float]:
    """Proposed SL floor in points for weighting w_m5 (0.0 = H1-only, 1.0 = M5-only)."""
    if trade.atr_m5 is None:
        return None
    weighted = w_m5 * trade.atr_m5 + (1.0 - w_m5) * trade.atr_h1
    return SPREAD_POINTS + weighted


def counterfactual_profit(trade: Trade, new_sl_dist: float, survivor_model: str = "mid_mfe") -> float:
    """Estimate P&L under counterfactual SL distance (in points).

    CRITICAL CORRECTION vs v1 of this script:
    H1-b widens the ORIGINAL SL at open. For trades whose actual close was
    triggered by TRAILING SL / BE (not original SL), H1-b has ZERO effect —
    trailing mechanism fires first regardless of where original SL was.
    Only trades with `hit_original_sl == True` are counterfactual-eligible.

    For eligible trades, `mae_points` is in PIPS (per mfe_backfill.py), so
    convert to points via ÷10 when comparing to new_sl_dist (in points).

    survivor_model (only applies when new_sl_dist > |MAE_points|):
      - "breakeven": survivor closes at $0
      - "small_win": survivor captures 0.25 × MFE
      - "mid_mfe": survivor captures 0.5 × MFE
    """
    # H1-b cannot affect trades that didn't hit the original SL.
    if not trade.hit_original_sl:
        return trade.profit or 0.0

    actual_sl_dist = trade.sl_dist_points
    if new_sl_dist <= actual_sl_dist + 1e-6:
        return trade.profit or 0.0

    usd_per_pt = trade.dollars_per_point

    # MAE units: mae_points is in PIPS (see mfe_backfill.py line 156).
    # Convert to points for comparison with new_sl_dist (points).
    mae_pts = abs(trade.mae_points) / 10.0 if trade.mae_points is not None else actual_sl_dist
    mfe_pts = abs(trade.mfe_points) / 10.0 if trade.mfe_points is not None else 0.0

    if mae_pts < new_sl_dist:
        # MAE within wider SL — trade survives (price didn't reach new SL)
        if survivor_model == "breakeven":
            return 0.0
        elif survivor_model == "small_win":
            return 0.25 * mfe_pts * usd_per_pt
        else:  # mid_mfe
            return 0.5 * mfe_pts * usd_per_pt
    else:
        # MAE ≥ new_sl — still stops at wider SL, bigger loss
        return -new_sl_dist * usd_per_pt


def model_b_new_sl_dist(trade: Trade, floor: float) -> float:
    """Partial adoption: SL moves halfway between actual and floor."""
    return 0.5 * (trade.sl_dist_points + floor)


def compute_metrics(profits: list[float], close_times: list[str]) -> dict[str, float]:
    profits_arr = np.array(profits, dtype=float)
    n = len(profits_arr)
    wins = int((profits_arr >= 0.5).sum())
    losses = int((profits_arr <= -0.5).sum())
    gross_win = float(profits_arr[profits_arr >= 0].sum())
    gross_loss = float(-profits_arr[profits_arr < 0].sum())
    pf = gross_win / gross_loss if gross_loss > 0 else float("inf")
    wr = 100.0 * wins / max(n, 1)
    avg_pnl = float(profits_arr.mean()) if n > 0 else 0.0

    # Max DD — cumulative P&L curve ordered by close_time
    order = np.argsort(close_times)
    cum = np.cumsum(profits_arr[order])
    running_peak = np.maximum.accumulate(cum)
    dd = running_peak - cum
    max_dd = float(dd.max()) if len(dd) else 0.0

    # Sharpe (daily) — group by close-date UTC
    from collections import defaultdict
    daily: dict[str, float] = defaultdict(float)
    for p, ct in zip(profits_arr, close_times):
        day = ct[:10] if ct else "unknown"
        daily[day] += float(p)
    returns = np.array(list(daily.values()))
    sharpe = float(returns.mean() / returns.std(ddof=1)) if len(returns) > 1 and returns.std(ddof=1) > 0 else 0.0

    return {
        "n": n,
        "wins": wins,
        "losses": losses,
        "wr_pct": round(wr, 2),
        "pf": round(pf, 3) if pf != float("inf") else -1,
        "avg_pnl_usd": round(avg_pnl, 3),
        "total_pnl_usd": round(float(profits_arr.sum()), 2),
        "max_dd_usd": round(max_dd, 2),
        "gross_win_usd": round(gross_win, 2),
        "gross_loss_usd": round(gross_loss, 2),
        "sharpe_daily": round(sharpe, 3),
        "n_days": len(daily),
    }


def bootstrap_pf(profits: list[float], n_resamples: int = 1000, seed: int = 42) -> tuple[float, float]:
    rng = np.random.default_rng(seed)
    arr = np.array(profits, dtype=float)
    if len(arr) == 0:
        return 0.0, 0.0
    pfs = []
    for _ in range(n_resamples):
        sample = arr[rng.integers(0, len(arr), size=len(arr))]
        gw = sample[sample >= 0].sum()
        gl = -sample[sample < 0].sum()
        if gl > 0:
            pfs.append(gw / gl)
    if not pfs:
        return 0.0, 0.0
    pfs.sort()
    return float(pfs[int(0.025 * len(pfs))]), float(pfs[int(0.975 * len(pfs))])


def run_weighting(trades: list[Trade], w_m5: float, label: str) -> dict:
    out: dict[str, Any] = {"weighting": label, "w_m5": w_m5}

    # Per-trade diagnostics — only original-SL hits are eligible
    widened = 0
    eligible_original_sl_hit = 0
    rescued = 0                  # eligible + widened + MAE fits under new SL
    still_stopped_wider = 0      # eligible + widened + MAE >= new SL
    unaffected_trail = 0         # trailing/BE exits — H1-b has no effect
    unaffected_floor_below = 0   # floor below actual SL — rule doesn't widen
    for t in trades:
        floor = floor_points(t, w_m5)
        if floor is None:
            continue
        if floor <= t.sl_dist_points:
            unaffected_floor_below += 1
            continue
        widened += 1
        if not t.hit_original_sl:
            unaffected_trail += 1
            continue
        eligible_original_sl_hit += 1
        mae_pts = abs(t.mae_points) / 10.0 if t.mae_points is not None else t.sl_dist_points
        if mae_pts < floor:
            rescued += 1
        else:
            still_stopped_wider += 1

    # SURVIVOR SENSITIVITY: run all 3 survivor models x 2 adoption models
    close_times = [t.close_time or "" for t in trades]
    variants = {}
    for survivor in ("breakeven", "small_win", "mid_mfe"):
        # Adoption = obedience (SL to floor)
        cf_full = [counterfactual_profit(t, floor_points(t, w_m5) or t.sl_dist_points, survivor)
                   for t in trades]
        # Adoption = partial 50% (SL to midpoint actual+floor)
        cf_half = [counterfactual_profit(
            t, model_b_new_sl_dist(t, floor_points(t, w_m5) or t.sl_dist_points), survivor)
            for t in trades]

        m_full = compute_metrics(cf_full, close_times)
        m_half = compute_metrics(cf_half, close_times)
        f_lo, f_hi = bootstrap_pf(cf_full)
        h_lo, h_hi = bootstrap_pf(cf_half)
        m_full["pf_95ci"] = [round(f_lo, 3), round(f_hi, 3)]
        m_half["pf_95ci"] = [round(h_lo, 3), round(h_hi, 3)]
        variants[survivor] = {
            "adopt_obedience": m_full,
            "adopt_partial_50": m_half,
        }

    out["widened_count"] = widened
    out["widened_pct"] = round(100 * widened / max(len(trades), 1), 1)
    out["eligible_original_sl_hit"] = eligible_original_sl_hit
    out["rescued"] = rescued
    out["still_stopped_at_wider_sl"] = still_stopped_wider
    out["unaffected_trail_or_be"] = unaffected_trail
    out["unaffected_floor_below"] = unaffected_floor_below
    out["survivor_variants"] = variants
    return out


def edge_case_subsets(trades: list[Trade]) -> dict[str, list[Trade]]:
    """Partition trades into simple edge-case buckets for qualitative check."""
    if not trades:
        return {}
    atr_vals = np.array([t.atr_h1 for t in trades])
    hi_cut = float(np.quantile(atr_vals, 0.9))
    lo_cut = float(np.quantile(atr_vals, 0.3))
    return {
        "high_vol_top10pct_atr": [t for t in trades if t.atr_h1 >= hi_cut],
        "low_vol_bottom30pct_atr": [t for t in trades if t.atr_h1 <= lo_cut],
        "asian_session": [t for t in trades if (t.session or "").upper() == "ASIAN"],
        "sub_half_atr_cohort": [t for t in trades if t.sl_dist_points < 0.5 * t.atr_h1],
    }


def main() -> int:
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)

    print("Loading trades...")
    all_trades = load_trades()
    print(f"  loaded {len(all_trades)} trades with SL>0 + closed + MFE/MAE + atr_h1")

    print("Reconstructing M5 ATR from MT5 candles...")
    reconstruct_m5_atr(all_trades)
    with_m5 = [t for t in all_trades if t.atr_m5 is not None]
    without_m5 = len(all_trades) - len(with_m5)
    print(f"  {len(with_m5)} trades have M5 ATR; {without_m5} missing (dropped)")

    trades = with_m5

    # BASELINE
    baseline_profits = [t.profit or 0.0 for t in trades]
    baseline_close_times = [t.close_time or "" for t in trades]
    baseline = compute_metrics(baseline_profits, baseline_close_times)
    b_lo, b_hi = bootstrap_pf(baseline_profits)
    baseline["pf_95ci"] = [round(b_lo, 3), round(b_hi, 3)]

    # WEIGHTING GRID
    weightings = {
        "0.0_H1_only": 0.0,
        "0.3_stability": 0.3,
        "0.5_balanced": 0.5,
        "0.7_reactive": 0.7,
        "1.0_M5_only": 1.0,
    }
    results = {}
    for label, w in weightings.items():
        print(f"  running weighting {label} ...")
        results[label] = run_weighting(trades, w, label)

    # EDGE CASES — run 0.5 balanced on subsets
    edge = {}
    edge_subsets = edge_case_subsets(trades)
    for bucket, bucket_trades in edge_subsets.items():
        if len(bucket_trades) < 5:
            edge[bucket] = {"n": len(bucket_trades), "note": "too small for bootstrap"}
            continue
        edge[bucket] = {
            "n": len(bucket_trades),
            "baseline_wr_pct": round(100 * sum(1 for t in bucket_trades if (t.profit or 0) > 0) / len(bucket_trades), 1),
            "baseline_total_pnl": round(sum(t.profit or 0 for t in bucket_trades), 2),
            "w_0.5_balanced": run_weighting(bucket_trades, 0.5, "0.5_balanced"),
        }

    # Distributional stats
    atr_ratios = [t.sl_dist_points / t.atr_h1 for t in trades if t.atr_h1 > 0]
    dist_stats = {
        "n_trades_final": len(trades),
        "sl_dist_points": {
            "min": round(min(t.sl_dist_points for t in trades), 2),
            "max": round(max(t.sl_dist_points for t in trades), 2),
            "median": round(float(np.median([t.sl_dist_points for t in trades])), 2),
            "p25": round(float(np.percentile([t.sl_dist_points for t in trades], 25)), 2),
            "p75": round(float(np.percentile([t.sl_dist_points for t in trades], 75)), 2),
        },
        "atr_h1_points": {
            "min": round(min(t.atr_h1 for t in trades), 2),
            "max": round(max(t.atr_h1 for t in trades), 2),
            "median": round(float(np.median([t.atr_h1 for t in trades])), 2),
        },
        "atr_m5_points": {
            "min": round(min(t.atr_m5 for t in trades), 2),
            "max": round(max(t.atr_m5 for t in trades), 2),
            "median": round(float(np.median([t.atr_m5 for t in trades])), 2),
        },
        "sl_over_h1_atr_ratio": {
            "median": round(float(np.median(atr_ratios)), 3),
            "p25": round(float(np.percentile(atr_ratios, 25)), 3),
            "p75": round(float(np.percentile(atr_ratios, 75)), 3),
            "pct_sub_half_atr": round(100.0 * sum(1 for r in atr_ratios if r < 0.5) / max(len(atr_ratios), 1), 1),
        },
    }

    out = {
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "methodology": {
            "spread_points_assumed": SPREAD_POINTS,
            "broker_offset_hours": BROKER_OFFSET_HOURS,
            "atr_period": ATR_PERIOD,
            "spread_note": "fixed 3.0 points — typical XAU normal-session. Real spread varies 2-8pts.",
            "m5_source": "reconstructed via MT5 copy_rates_from at open_time",
            "counterfactual_models": {
                "A_obedience": "SL widened to floor exactly if floor > actual",
                "B_partial_50": "SL moves halfway between actual and floor",
            },
            "survival_model": "if close_reason=='Stop Loss' and MAE < new_SL_dist: survive → 0.5*MFE*$/pt",
            "unit_note": "ATR values are in POINTS (not pips). 1 point = $1 per 0.01 lot for XAU.",
        },
        "data_window": {
            "design_doc_asked": "2025-10-22 → 2026-04-22 (6 months, 400-500 trades)",
            "actual_available": "2026-03-24 → 2026-04-22 (30 days)",
            "note": "6-month window was infeasible — data/history.db and data/trade_conditions/ only populated from late March 2026. Reconstructing ATR for older MT5 deals was out of scope (see report).",
        },
        "dist_stats": dist_stats,
        "baseline": baseline,
        "weightings": results,
        "edge_cases": edge,
        "go_no_go_thresholds": {
            "required": {"pf_delta_min": 0.20, "max_dd_not_worse_than_pct": 10},
            "preferred": {"wr_delta_pp_min": 5},
        },
    }

    with OUT_PATH.open("w") as f:
        json.dump(out, f, indent=2, default=str)
    print(f"\nWrote results to {OUT_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
