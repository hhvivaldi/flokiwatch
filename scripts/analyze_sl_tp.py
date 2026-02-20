"""
Dynamic SL/TP by Session & Volatility - Data Analysis
======================================================
Analyzes 662 backtest trades to evaluate SL/TP sizing patterns
across sessions and volatility levels.

Usage:
    python scripts/analyze_sl_tp.py
"""

import os
import sys
import pandas as pd
import numpy as np

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CSV_PATH = os.path.join(ROOT_DIR, "data", "backtest_trades_20260218_2147.csv")

PIP_SIZE = 0.1
SL_ATR_MULT = 1.5   # config: SL = 1.5 * ATR
TP_ATR_MULT = 3.0   # config: TP = 3.0 * ATR
MIN_SL_PIPS = 150
MAX_SL_PIPS = 800

# Session definitions (UTC hours)
SESSIONS = {
    "Asia":    (0, 7),
    "London":  (7, 16),
    "NY":      (13, 21),
    "Overlap": (13, 16),
}


def load_trades(csv_path: str) -> pd.DataFrame:
    df = pd.read_csv(csv_path)
    df['entry_time'] = pd.to_datetime(df['entry_time'])
    df['close_time'] = pd.to_datetime(df['close_time'])
    df['win'] = df['profit_pips'] > 0
    df['hour'] = df['entry_time'].dt.hour

    # Derive SL/TP distances in pips
    df['sl_pips'] = (abs(df['entry_price'] - df['sl']) / PIP_SIZE).round(1)
    df['tp_pips'] = (abs(df['tp'] - df['entry_price']) / PIP_SIZE).round(1)

    # Derive ATR from SL (SL = 1.5 * ATR, clamped to [150, 800])
    # If SL == MIN_SL_PIPS, ATR could be lower; if SL == MAX_SL_PIPS, ATR could be higher
    # Best estimate: ATR = SL_pips / 1.5 (accurate when not clamped)
    df['atr_est_pips'] = (df['sl_pips'] / SL_ATR_MULT).round(1)

    # SL as multiple of estimated ATR
    df['sl_atr_ratio'] = df['sl_pips'] / df['atr_est_pips']

    # TP reached ratio: how close did price get to TP?
    df['tp_reach_pct'] = (df['max_favorable_pips'] / df['tp_pips'] * 100).round(1)
    df['tp_reach_pct'] = df['tp_reach_pct'].clip(upper=100)

    # Session assignment
    def get_session(h):
        if 0 <= h < 7:
            return "Asia"
        elif 7 <= h < 13:
            return "London"
        elif 13 <= h < 16:
            return "Overlap"
        elif 16 <= h < 21:
            return "NY"
        else:
            return "Off-hours"
    df['session'] = df['hour'].apply(get_session)

    # SL size buckets
    def sl_bucket(sl):
        if sl <= 160:
            return "Small (<=160)"
        elif sl <= 250:
            return "Medium (161-250)"
        else:
            return "Large (>250)"
    df['sl_bucket'] = df['sl_pips'].apply(sl_bucket)

    return df


# ============================================================================
# 1. ATR BY SESSION
# ============================================================================

def analyze_atr_by_session(df: pd.DataFrame) -> None:
    print(f"\n{'=' * 100}")
    print(f"  AVERAGE ATR (ESTIMATED) BY SESSION")
    print(f"{'=' * 100}")
    print(f"  ATR estimated from SL distance: ATR = SL_pips / {SL_ATR_MULT}")
    print(f"  Note: ATR is clamped when SL hits MIN({MIN_SL_PIPS}) or MAX({MAX_SL_PIPS}) pips")
    print()
    print(f"  {'Session':>12}  {'Trades':>6}  {'Avg ATR':>8}  {'Med ATR':>8}  {'Min ATR':>8}  {'Max ATR':>8}  {'Avg SL':>7}  {'Avg TP':>7}")
    print(f"  {'-' * 12}  {'-' * 6}  {'-' * 8}  {'-' * 8}  {'-' * 8}  {'-' * 8}  {'-' * 7}  {'-' * 7}")

    for session in ["Asia", "London", "Overlap", "NY", "Off-hours"]:
        s_df = df[df['session'] == session]
        n = len(s_df)
        if n == 0:
            continue
        print(f"  {session:>12}  {n:>6}  {s_df['atr_est_pips'].mean():>7.0f}p  {s_df['atr_est_pips'].median():>7.0f}p"
              f"  {s_df['atr_est_pips'].min():>7.0f}p  {s_df['atr_est_pips'].max():>7.0f}p"
              f"  {s_df['sl_pips'].mean():>6.0f}p  {s_df['tp_pips'].mean():>6.0f}p")

    # Overall
    print(f"  {'OVERALL':>12}  {len(df):>6}  {df['atr_est_pips'].mean():>7.0f}p  {df['atr_est_pips'].median():>7.0f}p"
          f"  {df['atr_est_pips'].min():>7.0f}p  {df['atr_est_pips'].max():>7.0f}p"
          f"  {df['sl_pips'].mean():>6.0f}p  {df['tp_pips'].mean():>6.0f}p")
    print()


# ============================================================================
# 2. SL/TP DISTANCE BY SESSION
# ============================================================================

def analyze_sl_tp_by_session(df: pd.DataFrame) -> None:
    print(f"\n{'=' * 100}")
    print(f"  SL/TP DISTANCE AND PERFORMANCE BY SESSION")
    print(f"{'=' * 100}")
    print(f"  {'Session':>12}  {'Trades':>6}  {'WR%':>6}  {'Avg SL':>7}  {'Avg TP':>7}  {'RR':>5}  {'P&L $':>10}  {'PF':>6}  {'Avg fav':>8}  {'Avg adv':>8}")
    print(f"  {'-' * 12}  {'-' * 6}  {'-' * 6}  {'-' * 7}  {'-' * 7}  {'-' * 5}  {'-' * 10}  {'-' * 6}  {'-' * 8}  {'-' * 8}")

    for session in ["Asia", "London", "Overlap", "NY", "Off-hours"]:
        s_df = df[df['session'] == session]
        n = len(s_df)
        if n == 0:
            continue
        wins = s_df['win'].sum()
        wr = wins / n * 100
        avg_sl = s_df['sl_pips'].mean()
        avg_tp = s_df['tp_pips'].mean()
        rr = avg_tp / avg_sl if avg_sl > 0 else 0
        pnl = s_df['profit_usd'].sum()
        gw = s_df[s_df['win']]['profit_usd'].sum()
        gl = abs(s_df[~s_df['win']]['profit_usd'].sum())
        pf = gw / gl if gl > 0 else float('inf')
        avg_fav = s_df['max_favorable_pips'].mean()
        avg_adv = s_df['max_adverse_pips'].mean()
        print(f"  {session:>12}  {n:>6}  {wr:>5.1f}%  {avg_sl:>6.0f}p  {avg_tp:>6.0f}p  {rr:>5.2f}  ${pnl:>+9.2f}  {pf:>6.2f}  {avg_fav:>7.0f}p  {avg_adv:>7.0f}p")

    print()


# ============================================================================
# 3. WIN RATE BY SESSION AND SL SIZE BUCKET
# ============================================================================

def analyze_wr_by_session_sl(df: pd.DataFrame) -> None:
    print(f"\n{'=' * 100}")
    print(f"  WIN RATE BY SESSION x SL SIZE BUCKET")
    print(f"{'=' * 100}")

    buckets = ["Small (<=160)", "Medium (161-250)", "Large (>250)"]
    sessions = ["Asia", "London", "Overlap", "NY"]

    # Header
    header = f"  {'':>12}"
    for b in buckets:
        header += f"  {b:>18}"
    header += f"  {'ALL':>10}"
    print(header)
    print(f"  {'-' * 12}" + f"  {'-' * 18}" * len(buckets) + f"  {'-' * 10}")

    for session in sessions:
        row = f"  {session:>12}"
        s_df = df[df['session'] == session]
        for b in buckets:
            b_df = s_df[s_df['sl_bucket'] == b]
            n = len(b_df)
            if n >= 5:
                wr = b_df['win'].mean() * 100
                row += f"  {wr:>5.1f}% ({n:>3}t)"
            elif n > 0:
                wr = b_df['win'].mean() * 100
                row += f"  {wr:>5.1f}% ({n:>3}t)*"
            else:
                row += f"  {'---':>18}"
        # Session total
        n = len(s_df)
        wr = s_df['win'].mean() * 100 if n > 0 else 0
        row += f"  {wr:>5.1f}% ({n})"
        print(row)

    # Overall by bucket
    row = f"  {'ALL':>12}"
    for b in buckets:
        b_df = df[df['sl_bucket'] == b]
        n = len(b_df)
        wr = b_df['win'].mean() * 100 if n > 0 else 0
        row += f"  {wr:>5.1f}% ({n:>3}t)"
    row += f"  {df['win'].mean() * 100:>5.1f}% ({len(df)})"
    print(row)

    print(f"\n  * = sample size < 5, interpret with caution")
    print()


# ============================================================================
# 4. QUICK SL HITS (SL within first 30 min)
# ============================================================================

def analyze_quick_sl_hits(df: pd.DataFrame) -> None:
    # Trades that hit SL
    sl_trades = df[df['close_reason'].isin(['sl', 'sl_gap'])]
    total_sl = len(sl_trades)

    # Quick SL: duration <= 30 min
    quick_sl = sl_trades[sl_trades['duration_min'] <= 30]
    n_quick = len(quick_sl)

    print(f"\n{'=' * 100}")
    print(f"  QUICK SL HITS (closed by SL within 30 minutes)")
    print(f"{'=' * 100}")
    print(f"  Total SL hits: {total_sl} ({total_sl / len(df) * 100:.1f}% of all trades)")
    print(f"  Quick SL (<=30 min): {n_quick} ({n_quick / total_sl * 100:.1f}% of SL hits, {n_quick / len(df) * 100:.1f}% of all trades)")
    print()

    if n_quick > 0:
        print(f"  Quick SL breakdown by session:")
        print(f"  {'Session':>12}  {'Quick SL':>8}  {'Total SL':>8}  {'% Quick':>8}  {'Avg SL pips':>12}  {'Avg ATR':>8}")
        print(f"  {'-' * 12}  {'-' * 8}  {'-' * 8}  {'-' * 8}  {'-' * 12}  {'-' * 8}")
        for session in ["Asia", "London", "Overlap", "NY", "Off-hours"]:
            s_sl = sl_trades[sl_trades['session'] == session]
            s_quick = quick_sl[quick_sl['session'] == session]
            n_s = len(s_sl)
            n_q = len(s_quick)
            if n_s == 0:
                continue
            pct = n_q / n_s * 100 if n_s > 0 else 0
            avg_sl = s_quick['sl_pips'].mean() if n_q > 0 else 0
            avg_atr = s_quick['atr_est_pips'].mean() if n_q > 0 else 0
            print(f"  {session:>12}  {n_q:>8}  {n_s:>8}  {pct:>7.1f}%  {avg_sl:>11.0f}p  {avg_atr:>7.0f}p")

        print()

        # SL size distribution of quick hits vs normal SL hits
        normal_sl = sl_trades[sl_trades['duration_min'] > 30]
        print(f"  SL size comparison:")
        print(f"    Quick SL (<=30 min): avg {quick_sl['sl_pips'].mean():.0f}p, median {quick_sl['sl_pips'].median():.0f}p")
        print(f"    Normal SL (>30 min): avg {normal_sl['sl_pips'].mean():.0f}p, median {normal_sl['sl_pips'].median():.0f}p")

        # Duration distribution of SL hits
        print(f"\n  SL hit timing distribution:")
        time_buckets = [(0, 30, "0-30 min"), (30, 60, "30-60 min"), (60, 120, "1-2 hours"),
                        (120, 240, "2-4 hours"), (240, 480, "4-8 hours"), (480, 99999, "8+ hours")]
        print(f"  {'Duration':>12}  {'Count':>6}  {'%':>6}  {'Avg SL':>8}")
        print(f"  {'-' * 12}  {'-' * 6}  {'-' * 6}  {'-' * 8}")
        for lo, hi, label in time_buckets:
            bucket = sl_trades[(sl_trades['duration_min'] > lo) | (lo == 0)]
            bucket = sl_trades[(sl_trades['duration_min'] >= lo) & (sl_trades['duration_min'] < hi)]
            n = len(bucket)
            if n == 0:
                continue
            pct = n / total_sl * 100
            print(f"  {label:>12}  {n:>6}  {pct:>5.1f}%  {bucket['sl_pips'].mean():>7.0f}p")

    print()


# ============================================================================
# 5. TP REACH ANALYSIS (price never came close to TP)
# ============================================================================

def analyze_tp_reach(df: pd.DataFrame) -> None:
    # Only look at losing trades (winners reached TP or close to it)
    losers = df[~df['win']]
    total_losers = len(losers)

    print(f"\n{'=' * 100}")
    print(f"  TP REACH ANALYSIS (how close did price get to TP before losing?)")
    print(f"{'=' * 100}")
    print(f"  Total losing trades: {total_losers}")
    print()

    # TP reach buckets for losers
    reach_buckets = [(0, 10, "0-10% (never close)"), (10, 25, "10-25%"), (25, 50, "25-50%"),
                     (50, 75, "50-75%"), (75, 100, "75-99% (almost hit)")]
    print(f"  How far did losing trades get toward TP?")
    print(f"  {'TP Reach':>22}  {'Count':>6}  {'%':>6}  {'Avg SL':>8}  {'Avg TP':>8}  {'Avg dur':>8}")
    print(f"  {'-' * 22}  {'-' * 6}  {'-' * 6}  {'-' * 8}  {'-' * 8}  {'-' * 8}")

    for lo, hi, label in reach_buckets:
        bucket = losers[(losers['tp_reach_pct'] >= lo) & (losers['tp_reach_pct'] < hi)]
        n = len(bucket)
        if n == 0:
            continue
        pct = n / total_losers * 100
        avg_sl = bucket['sl_pips'].mean()
        avg_tp = bucket['tp_pips'].mean()
        avg_dur = bucket['duration_min'].mean()
        print(f"  {label:>22}  {n:>6}  {pct:>5.1f}%  {avg_sl:>7.0f}p  {avg_tp:>7.0f}p  {avg_dur:>7.0f}m")

    # Trades where price never reached even 25% of TP
    never_close = losers[losers['tp_reach_pct'] < 25]
    print(f"\n  Trades that never reached 25% of TP: {len(never_close)} ({len(never_close) / total_losers * 100:.1f}% of losers)")
    if len(never_close) > 0:
        print(f"    Avg SL: {never_close['sl_pips'].mean():.0f}p | Avg TP: {never_close['tp_pips'].mean():.0f}p | Avg duration: {never_close['duration_min'].mean():.0f}m")
        print(f"    By session:")
        for session in ["Asia", "London", "Overlap", "NY"]:
            s_nc = never_close[never_close['session'] == session]
            if len(s_nc) > 0:
                print(f"      {session:>10}: {len(s_nc)} trades")

    # Winners: how many actually hit TP vs trailing/other
    winners = df[df['win']]
    tp_winners = winners[winners['close_reason'].isin(['tp', 'tp_gap'])]
    trail_winners = winners[~winners['close_reason'].isin(['tp', 'tp_gap'])]
    print(f"\n  Winner exit analysis:")
    print(f"    Hit TP: {len(tp_winners)} ({len(tp_winners) / len(winners) * 100:.1f}%)")
    print(f"    Other (trailing/duration): {len(trail_winners)} ({len(trail_winners) / len(winners) * 100:.1f}%)")
    if len(trail_winners) > 0:
        print(f"    Non-TP winners avg TP reach: {trail_winners['tp_reach_pct'].mean():.1f}%")

    print()


# ============================================================================
# 6. SL SIZE vs ATR RATIO FOR LOSERS
# ============================================================================

def analyze_sl_atr_ratio(df: pd.DataFrame) -> None:
    losers = df[~df['win']]
    total_losers = len(losers)

    print(f"\n{'=' * 100}")
    print(f"  SL/ATR RATIO ANALYSIS FOR LOSING TRADES")
    print(f"{'=' * 100}")
    print(f"  Current config: SL = {SL_ATR_MULT}x ATR (clamped to [{MIN_SL_PIPS}, {MAX_SL_PIPS}] pips)")
    print(f"  Note: When SL = MIN_SL_PIPS ({MIN_SL_PIPS}), the effective SL/ATR ratio is exactly {SL_ATR_MULT}")
    print(f"  because ATR is estimated from SL. True ATR may be lower, making effective ratio higher.")
    print()

    # Since SL = 1.5 * ATR (clamped), and we estimate ATR = SL/1.5,
    # the ratio is always 1.5 unless SL was clamped.
    # Better approach: look at SL size relative to actual price movement
    # Use max_adverse_pips as proxy for "how much room was needed"

    # SL vs max_adverse analysis
    print(f"  SL tightness analysis (SL vs actual adverse movement):")
    print(f"  {'Category':>30}  {'Count':>6}  {'% Losers':>9}  {'Avg SL':>8}  {'Avg Adverse':>12}")
    print(f"  {'-' * 30}  {'-' * 6}  {'-' * 9}  {'-' * 8}  {'-' * 12}")

    # Losers where adverse == SL (hit SL exactly, no room)
    sl_exact = losers[abs(losers['max_adverse_pips'] - losers['sl_pips']) < 5]
    # Losers where adverse < 0.5 * SL (barely moved against, then reversed past SL?)
    # Actually for SL hits, max_adverse >= SL by definition

    # Better: look at SL bucket distribution for losers
    buckets = [
        ("SL <= 150p (at minimum)", 0, 151),
        ("SL 151-200p", 151, 201),
        ("SL 201-300p", 201, 301),
        ("SL > 300p", 301, 9999),
    ]
    for label, lo, hi in buckets:
        b = losers[(losers['sl_pips'] >= lo) & (losers['sl_pips'] < hi)]
        n = len(b)
        if n == 0:
            continue
        pct = n / total_losers * 100
        print(f"  {label:>30}  {n:>6}  {pct:>8.1f}%  {b['sl_pips'].mean():>7.0f}p  {b['max_adverse_pips'].mean():>11.0f}p")

    # Key question: what % of losers had SL at the minimum (150 pips)?
    at_min = losers[losers['sl_pips'] <= 151]
    above_min = losers[losers['sl_pips'] > 151]
    print(f"\n  Losers at MIN_SL ({MIN_SL_PIPS}p): {len(at_min)} ({len(at_min) / total_losers * 100:.1f}%)")
    print(f"  Losers above MIN_SL:        {len(above_min)} ({len(above_min) / total_losers * 100:.1f}%)")

    if len(at_min) > 0 and len(above_min) > 0:
        # Compare: are min-SL trades losing more often?
        all_at_min = df[df['sl_pips'] <= 151]
        all_above_min = df[df['sl_pips'] > 151]
        wr_at_min = all_at_min['win'].mean() * 100
        wr_above_min = all_above_min['win'].mean() * 100
        print(f"\n  Win rate comparison:")
        print(f"    SL at minimum ({MIN_SL_PIPS}p): WR {wr_at_min:.1f}% ({len(all_at_min)} trades)")
        print(f"    SL above minimum:        WR {wr_above_min:.1f}% ({len(all_above_min)} trades)")
        diff = wr_at_min - wr_above_min
        if diff < -5:
            print(f"    --> MIN_SL trades have {abs(diff):.1f}pp LOWER WR - SL may be too tight when clamped")
        elif diff > 5:
            print(f"    --> MIN_SL trades have {diff:.1f}pp HIGHER WR")
        else:
            print(f"    --> Difference is small ({diff:+.1f}pp) - SL clamping not a major issue")

    # What % of losers had SL < 1.5x ATR vs > 2.5x ATR?
    # Since we estimate ATR = SL/1.5, all trades have ratio ~1.5
    # Instead, use a different proxy: compare SL to median SL
    median_sl = df['sl_pips'].median()
    tight_sl = losers[losers['sl_pips'] < median_sl * 0.8]
    wide_sl = losers[losers['sl_pips'] > median_sl * 1.5]
    normal_sl = losers[(losers['sl_pips'] >= median_sl * 0.8) & (losers['sl_pips'] <= median_sl * 1.5)]

    print(f"\n  SL relative to median ({median_sl:.0f}p):")
    print(f"  {'Category':>25}  {'Losers':>7}  {'% of losers':>12}  {'All trades':>10}  {'WR%':>6}")
    print(f"  {'-' * 25}  {'-' * 7}  {'-' * 12}  {'-' * 10}  {'-' * 6}")

    for label, l_df in [("Tight (<80% median)", tight_sl), ("Normal (80-150%)", normal_sl), ("Wide (>150% median)", wide_sl)]:
        n_l = len(l_df)
        pct_l = n_l / total_losers * 100 if total_losers > 0 else 0
        # Get all trades in same SL range for WR
        if "Tight" in label:
            all_in_range = df[df['sl_pips'] < median_sl * 0.8]
        elif "Wide" in label:
            all_in_range = df[df['sl_pips'] > median_sl * 1.5]
        else:
            all_in_range = df[(df['sl_pips'] >= median_sl * 0.8) & (df['sl_pips'] <= median_sl * 1.5)]
        n_all = len(all_in_range)
        wr = all_in_range['win'].mean() * 100 if n_all > 0 else 0
        print(f"  {label:>25}  {n_l:>7}  {pct_l:>11.1f}%  {n_all:>10}  {wr:>5.1f}%")

    print()


# ============================================================================
# MAIN
# ============================================================================

def main():
    if not os.path.exists(CSV_PATH):
        print(f"CSV not found: {CSV_PATH}")
        return

    df = load_trades(CSV_PATH)
    print(f"Loaded {len(df)} trades from CSV")
    print(f"SL range: {df['sl_pips'].min():.0f} - {df['sl_pips'].max():.0f} pips (median: {df['sl_pips'].median():.0f})")
    print(f"TP range: {df['tp_pips'].min():.0f} - {df['tp_pips'].max():.0f} pips (median: {df['tp_pips'].median():.0f})")
    print(f"ATR est range: {df['atr_est_pips'].min():.0f} - {df['atr_est_pips'].max():.0f} pips (median: {df['atr_est_pips'].median():.0f})")

    analyze_atr_by_session(df)
    analyze_sl_tp_by_session(df)
    analyze_wr_by_session_sl(df)
    analyze_quick_sl_hits(df)
    analyze_tp_reach(df)
    analyze_sl_atr_ratio(df)

    # Final summary
    print(f"{'=' * 100}")
    print(f"  SUMMARY")
    print(f"{'=' * 100}")

    # Key stats
    sl_trades = df[df['close_reason'].isin(['sl', 'sl_gap'])]
    quick_sl = sl_trades[sl_trades['duration_min'] <= 30]
    losers = df[~df['win']]
    never_close_tp = losers[losers['tp_reach_pct'] < 25]
    at_min_sl = df[df['sl_pips'] <= 151]

    print(f"  Quick SL hits (<=30 min): {len(quick_sl)}/{len(sl_trades)} SL trades ({len(quick_sl) / len(sl_trades) * 100:.1f}%)")
    print(f"  Losers never reaching 25% of TP: {len(never_close_tp)}/{len(losers)} ({len(never_close_tp) / len(losers) * 100:.1f}%)")
    print(f"  Trades at MIN_SL ({MIN_SL_PIPS}p): {len(at_min_sl)}/{len(df)} ({len(at_min_sl) / len(df) * 100:.1f}%)")
    wr_min = at_min_sl['win'].mean() * 100
    wr_other = df[df['sl_pips'] > 151]['win'].mean() * 100
    print(f"  WR at MIN_SL: {wr_min:.1f}% vs WR above MIN_SL: {wr_other:.1f}%")
    print()


if __name__ == "__main__":
    main()
