"""
Dynamic SL/TP Deep Dive - MIN_SL Sensitivity & TP Contribution
==============================================================
Follow-up analysis for SL/TP optimization.

Usage:
    python scripts/analyze_sl_deep.py
"""

import os
import sys
import pandas as pd
import numpy as np

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CSV_PATH = os.path.join(ROOT_DIR, "data", "backtest_trades_20260218_2147.csv")

PIP_SIZE = 0.1
SL_ATR_MULT = 1.5
TP1_ATR_MULT = 3.0
MIN_SL_PIPS = 150
MAX_SL_PIPS = 800


def load_trades(csv_path: str) -> pd.DataFrame:
    df = pd.read_csv(csv_path)
    df['entry_time'] = pd.to_datetime(df['entry_time'])
    df['close_time'] = pd.to_datetime(df['close_time'])
    df['win'] = df['profit_pips'] > 0
    df['hour'] = df['entry_time'].dt.hour

    # SL/TP distances in pips
    df['sl_pips'] = (abs(df['entry_price'] - df['sl']) / PIP_SIZE).round(1)
    df['tp_pips'] = (abs(df['tp'] - df['entry_price']) / PIP_SIZE).round(1)

    # Is this trade clamped at MIN_SL?
    df['is_clamped'] = df['sl_pips'] <= 151  # small tolerance for rounding

    # For unclamped trades: ATR = SL / 1.5 (exact)
    # For clamped trades: ATR < 100p (unknown exact value)
    df['atr_est'] = df['sl_pips'] / SL_ATR_MULT

    # TP reach
    df['tp_reach_pct'] = (df['max_favorable_pips'] / df['tp_pips'] * 100).clip(upper=100).round(1)

    return df


# ============================================================================
# 1. ATR DISTRIBUTION OF CLAMPED TRADES
# ============================================================================

def analyze_clamped_atr(df: pd.DataFrame) -> None:
    clamped = df[df['is_clamped']]
    unclamped = df[~df['is_clamped']]

    print(f"\n{'=' * 100}")
    print(f"  CLAMPED vs UNCLAMPED TRADE ANALYSIS")
    print(f"{'=' * 100}")
    print(f"  SL formula: SL = ATR x {SL_ATR_MULT}, clamped to [{MIN_SL_PIPS}, {MAX_SL_PIPS}] pips")
    print(f"  Clamping occurs when ATR < {MIN_SL_PIPS / SL_ATR_MULT:.0f}p (raw SL would be < {MIN_SL_PIPS}p)")
    print()

    print(f"  {'Category':>20}  {'Trades':>6}  {'%':>6}  {'WR%':>6}  {'P&L $':>10}  {'PF':>6}  {'Avg SL':>7}  {'Avg TP':>7}")
    print(f"  {'-' * 20}  {'-' * 6}  {'-' * 6}  {'-' * 6}  {'-' * 10}  {'-' * 6}  {'-' * 7}  {'-' * 7}")

    for label, subset in [("Clamped (SL=150p)", clamped), ("Unclamped (SL>150p)", unclamped), ("ALL", df)]:
        n = len(subset)
        if n == 0:
            continue
        wr = subset['win'].mean() * 100
        pnl = subset['profit_usd'].sum()
        wins = subset[subset['win']]
        losses = subset[~subset['win']]
        gw = wins['profit_usd'].sum() if len(wins) > 0 else 0
        gl = abs(losses['profit_usd'].sum()) if len(losses) > 0 else 0
        pf = gw / gl if gl > 0 else float('inf')
        avg_sl = subset['sl_pips'].mean()
        avg_tp = subset['tp_pips'].mean()
        pct = n / len(df) * 100
        print(f"  {label:>20}  {n:>6}  {pct:>5.1f}%  {wr:>5.1f}%  ${pnl:>+9.2f}  {pf:>6.2f}  {avg_sl:>6.0f}p  {avg_tp:>6.0f}p")

    # Unclamped ATR distribution
    print(f"\n  Unclamped trades - ATR distribution (ATR = SL / {SL_ATR_MULT}):")
    print(f"  {'ATR Range':>14}  {'Trades':>6}  {'WR%':>6}  {'P&L $':>10}  {'Avg SL':>7}")
    print(f"  {'-' * 14}  {'-' * 6}  {'-' * 6}  {'-' * 10}  {'-' * 7}")

    atr_buckets = [(100, 120), (120, 150), (150, 200), (200, 300), (300, 600)]
    for lo, hi in atr_buckets:
        b = unclamped[(unclamped['atr_est'] >= lo) & (unclamped['atr_est'] < hi)]
        n = len(b)
        if n == 0:
            continue
        wr = b['win'].mean() * 100
        pnl = b['profit_usd'].sum()
        avg_sl = b['sl_pips'].mean()
        print(f"  {f'{lo}-{hi}p':>14}  {n:>6}  {wr:>5.1f}%  ${pnl:>+9.2f}  {avg_sl:>6.0f}p")

    # Clamped trades: what would their raw SL have been?
    # We can't know exactly, but we know ATR < 100p
    # The SL was forced UP from raw_SL to 150p
    # This means the SL is WIDER than ATR would dictate
    # Counterintuitive: clamping makes SL wider, not tighter!
    print(f"\n  IMPORTANT INSIGHT:")
    print(f"  When ATR < {MIN_SL_PIPS / SL_ATR_MULT:.0f}p, raw SL = ATR x {SL_ATR_MULT} < {MIN_SL_PIPS}p")
    print(f"  The MIN_SL clamp FORCES SL to {MIN_SL_PIPS}p, which is WIDER than ATR suggests.")
    print(f"  So clamped trades have an SL that is proportionally TOO WIDE relative to ATR.")
    print(f"  The real question: is the SL too wide (wasting risk) or is the market too calm")
    print(f"  for the bot's strategy (low ATR = ranging market = bad for trend-following)?")

    # Check: do clamped trades have different close reasons?
    print(f"\n  Close reason distribution:")
    print(f"  {'Reason':>15}  {'Clamped':>10}  {'Unclamped':>10}")
    print(f"  {'-' * 15}  {'-' * 10}  {'-' * 10}")
    for reason in ['sl', 'sl_gap', 'tp', 'tp_gap', 'max_duration', 'end_of_data']:
        c_n = len(clamped[clamped['close_reason'] == reason])
        u_n = len(unclamped[unclamped['close_reason'] == reason])
        c_pct = c_n / len(clamped) * 100 if len(clamped) > 0 else 0
        u_pct = u_n / len(unclamped) * 100 if len(unclamped) > 0 else 0
        print(f"  {reason:>15}  {c_pct:>8.1f}% ({c_n})  {u_pct:>8.1f}% ({u_n})")

    # Session distribution of clamped trades
    print(f"\n  Clamped trades by session:")
    def get_session(h):
        if 0 <= h < 7: return "Asia"
        elif 7 <= h < 13: return "London"
        elif 13 <= h < 16: return "Overlap"
        elif 16 <= h < 21: return "NY"
        else: return "Off-hours"
    clamped_sessions = clamped['hour'].apply(get_session)
    all_sessions = df['hour'].apply(get_session)
    for session in ["Asia", "London", "Overlap", "NY", "Off-hours"]:
        c_n = (clamped_sessions == session).sum()
        a_n = (all_sessions == session).sum()
        pct = c_n / a_n * 100 if a_n > 0 else 0
        print(f"    {session:>10}: {c_n}/{a_n} trades clamped ({pct:.1f}%)")

    print()


# ============================================================================
# 2. MIN_SL SENSITIVITY TEST
# ============================================================================

def sensitivity_test(df: pd.DataFrame) -> None:
    """
    We can't re-run the backtest with different MIN_SL values, but we CAN
    analyze what would change:
    - Lower MIN_SL: some currently-clamped trades would get a smaller SL
      (closer to entry). This means they'd be stopped out MORE often (tighter SL).
    - Higher MIN_SL: more trades get clamped to a wider SL. They'd survive
      more adverse moves but risk more per trade.

    Since we have max_adverse_pips for each trade, we can simulate:
    "If SL had been X pips, would this trade still have survived?"
    For winners: would they still win? (max_adverse < new_SL)
    For losers that hit SL: would a different SL have changed the outcome?
    """
    print(f"\n{'=' * 100}")
    print(f"  MIN_SL SENSITIVITY TEST")
    print(f"{'=' * 100}")
    print(f"  Method: For each MIN_SL value, check if trades' max_adverse_pips < SL.")
    print(f"  If max_adverse >= SL, the trade would have been stopped out regardless.")
    print(f"  If max_adverse < new_SL (but >= old_SL), the trade might have survived.")
    print()

    # Only analyze clamped trades (SL=150p) since those are the ones affected
    clamped = df[df['is_clamped']].copy()
    total_clamped = len(clamped)

    test_values = [100, 120, 130, 140, 150, 160, 180, 200]

    print(f"  Currently clamped trades (SL=150p): {total_clamped}")
    print()
    print(f"  {'MIN_SL':>8}  {'Clamped':>8}  {'WR (clamp)':>10}  {'WR (all)':>8}  {'Saved*':>6}  {'Lost*':>6}  {'Net chg':>8}")
    print(f"  {'-' * 8}  {'-' * 8}  {'-' * 10}  {'-' * 8}  {'-' * 6}  {'-' * 6}  {'-' * 8}")

    for min_sl in test_values:
        # How many trades would be clamped at this MIN_SL?
        # A trade is clamped when raw_SL < MIN_SL, i.e., ATR < MIN_SL / 1.5
        # For currently clamped trades (ATR < 100p): they'd still be clamped at any MIN_SL > 0
        # For currently unclamped trades: they'd become clamped if their SL < new MIN_SL
        would_be_clamped = df[df['sl_pips'] <= min_sl + 1]  # +1 for rounding
        n_clamped = len(would_be_clamped)

        # For the currently clamped trades, simulate with new SL
        # If MIN_SL goes DOWN (e.g., 120): SL gets tighter for clamped trades
        #   - Trades where max_adverse > 120 but < 150: these would NOW be stopped out
        #     (they survived with SL=150 but wouldn't with SL=120)
        #   - Trades where max_adverse <= 120: no change (SL wasn't reached anyway)
        # If MIN_SL goes UP (e.g., 180): SL gets wider
        #   - Trades that hit SL at 150: if max_adverse < 180, they might survive
        #     BUT we don't know what happens after — they might still lose later

        # Simpler approach: count trades that would flip outcome
        # "Saved" = currently a loser (hit SL at 150p) but max_adverse < new_MIN_SL
        #           (wider SL would have saved them — they might have won)
        # "Lost" = currently a winner but max_adverse is between new_MIN_SL and 150
        #          (tighter SL would have stopped them out before they could win)

        clamped_losers_sl = clamped[(~clamped['win']) & (clamped['close_reason'].isin(['sl', 'sl_gap']))]
        clamped_winners = clamped[clamped['win']]

        if min_sl < MIN_SL_PIPS:
            # Tighter SL: some winners might become losers
            # Winners where max_adverse is between new_min_sl and 150
            lost = len(clamped_winners[(clamped_winners['max_adverse_pips'] >= min_sl) &
                                       (clamped_winners['max_adverse_pips'] < MIN_SL_PIPS)])
            saved = 0  # Can't save losers with tighter SL
        elif min_sl > MIN_SL_PIPS:
            # Wider SL: some losers might be saved
            # Losers where max_adverse < new_min_sl (SL wouldn't have been hit)
            # BUT: these trades still lost — they hit SL at 150p, meaning adverse >= 150
            # With wider SL, adverse might go further. We can't know for sure.
            # Best proxy: losers whose max_adverse is between 150 and new_min_sl
            # These would NOT have been stopped out at the wider SL
            saved = len(clamped_losers_sl[(clamped_losers_sl['max_adverse_pips'] >= MIN_SL_PIPS - 5) &
                                          (clamped_losers_sl['max_adverse_pips'] < min_sl)])
            lost = 0  # Wider SL doesn't stop out winners
        else:
            saved = 0
            lost = 0

        # WR of would-be-clamped trades
        wr_clamped = would_be_clamped['win'].mean() * 100 if n_clamped > 0 else 0
        wr_all = df['win'].mean() * 100

        net = saved - lost
        print(f"  {min_sl:>7}p  {n_clamped:>8}  {wr_clamped:>9.1f}%  {wr_all:>7.1f}%  {saved:>6}  {lost:>6}  {net:>+7}")

    print(f"\n  * Saved = losers that might survive with wider SL")
    print(f"  * Lost = winners that would be stopped out with tighter SL")
    print(f"  Note: 'Saved' is optimistic — surviving SL doesn't guarantee a win.")

    # More detailed analysis: for clamped losers, how far past SL did price go?
    clamped_losers = clamped[~clamped['win']]
    print(f"\n  Clamped losers ({len(clamped_losers)}) - max adverse beyond SL:")
    overshoot = clamped_losers['max_adverse_pips'] - clamped_losers['sl_pips']
    print(f"    Avg overshoot past SL: {overshoot.mean():+.0f}p")
    print(f"    Median overshoot: {overshoot.median():+.0f}p")

    # How many clamped losers had adverse barely past SL (within 20 pips)?
    barely_past = clamped_losers[overshoot.abs() <= 20]
    print(f"    Losers where adverse was within 20p of SL: {len(barely_past)} ({len(barely_past) / len(clamped_losers) * 100:.1f}%)")
    print(f"    (These are the trades most likely to benefit from wider SL)")

    print()


# ============================================================================
# 3. TP CONTRIBUTION ANALYSIS
# ============================================================================

def analyze_tp_contribution(df: pd.DataFrame) -> None:
    winners = df[df['win']]
    total_winners = len(winners)
    total_winner_pnl = winners['profit_usd'].sum()

    tp_winners = winners[winners['close_reason'].isin(['tp', 'tp_gap'])]
    non_tp_winners = winners[~winners['close_reason'].isin(['tp', 'tp_gap'])]

    print(f"\n{'=' * 100}")
    print(f"  TP CONTRIBUTION ANALYSIS")
    print(f"{'=' * 100}")
    print(f"  Total winners: {total_winners}, total winner P&L: ${total_winner_pnl:+.2f}")
    print()

    print(f"  {'Exit Type':>25}  {'Count':>6}  {'%':>6}  {'P&L $':>12}  {'% of P&L':>9}  {'Avg pips':>9}  {'Avg dur':>8}")
    print(f"  {'-' * 25}  {'-' * 6}  {'-' * 6}  {'-' * 12}  {'-' * 9}  {'-' * 9}  {'-' * 8}")

    for label, subset in [("Hit TP", tp_winners), ("Trailing/Duration/Other", non_tp_winners)]:
        n = len(subset)
        pct = n / total_winners * 100
        pnl = subset['profit_usd'].sum()
        pnl_pct = pnl / total_winner_pnl * 100 if total_winner_pnl > 0 else 0
        avg_pips = subset['profit_pips'].mean()
        avg_dur = subset['duration_min'].mean()
        print(f"  {label:>25}  {n:>6}  {pct:>5.1f}%  ${pnl:>+11.2f}  {pnl_pct:>8.1f}%  {avg_pips:>+8.1f}  {avg_dur:>7.0f}m")

    # TP winners: how much more did they earn vs non-TP winners?
    if len(tp_winners) > 0 and len(non_tp_winners) > 0:
        tp_avg = tp_winners['profit_pips'].mean()
        non_tp_avg = non_tp_winners['profit_pips'].mean()
        print(f"\n  Avg profit: TP winners = {tp_avg:+.1f} pips vs Non-TP winners = {non_tp_avg:+.1f} pips")
        print(f"  TP winners earn {tp_avg / non_tp_avg:.1f}x more pips per trade")

    # What if we removed TP entirely (trailing stop only)?
    # TP winners would instead exit via trailing stop at ~58.9% of TP distance
    # (from previous analysis: non-TP winners avg reach = 58.9%)
    # But TP winners reached 100% — so they'd lose the last ~41% of TP distance
    print(f"\n  Scenario: Remove TP (trailing stop only)")
    if len(tp_winners) > 0:
        # TP winners currently get full TP distance
        # Without TP, they'd exit via trailing at some point before TP
        # Best estimate: they'd capture ~80% of TP (trailing triggers at ~1x ATR from peak)
        # Actually, we can estimate better: non-TP winners capture avg 58.9% of TP
        # But TP winners are the ones where price reached TP — they're the strong movers
        # They'd likely capture more than 58.9% via trailing

        # Conservative estimate: trailing captures 70% of what TP captured
        tp_pnl = tp_winners['profit_usd'].sum()
        est_trailing_pnl = tp_pnl * 0.70
        lost_pnl = tp_pnl - est_trailing_pnl

        print(f"    Current TP winner P&L: ${tp_pnl:+.2f}")
        print(f"    Estimated trailing capture (70%): ${est_trailing_pnl:+.2f}")
        print(f"    Estimated P&L loss: ${lost_pnl:.2f} ({lost_pnl / total_winner_pnl * 100:.1f}% of total winner P&L)")

    # TP distance analysis for winners
    print(f"\n  Winner profit distribution by close reason:")
    print(f"  {'Close Reason':>15}  {'Count':>6}  {'Avg profit pips':>15}  {'Avg TP dist':>12}  {'Profit/TP%':>11}")
    print(f"  {'-' * 15}  {'-' * 6}  {'-' * 15}  {'-' * 12}  {'-' * 11}")

    for reason in ['tp', 'tp_gap', 'sl', 'sl_gap', 'max_duration']:
        w = winners[winners['close_reason'] == reason]
        n = len(w)
        if n == 0:
            continue
        avg_profit = w['profit_pips'].mean()
        avg_tp = w['tp_pips'].mean()
        capture_pct = avg_profit / avg_tp * 100 if avg_tp > 0 else 0
        print(f"  {reason:>15}  {n:>6}  {avg_profit:>+14.1f}  {avg_tp:>11.0f}p  {capture_pct:>10.1f}%")

    # Non-TP winners: what % of TP did they capture?
    if len(non_tp_winners) > 0:
        capture = (non_tp_winners['profit_pips'] / non_tp_winners['tp_pips'] * 100)
        print(f"\n  Non-TP winners - profit as % of TP distance:")
        print(f"    Mean: {capture.mean():.1f}%")
        print(f"    Median: {capture.median():.1f}%")
        print(f"    25th percentile: {capture.quantile(0.25):.1f}%")
        print(f"    75th percentile: {capture.quantile(0.75):.1f}%")

    print()


# ============================================================================
# MAIN
# ============================================================================

def main():
    if not os.path.exists(CSV_PATH):
        print(f"CSV not found: {CSV_PATH}")
        return

    df = load_trades(CSV_PATH)
    print(f"Loaded {len(df)} trades")
    print(f"Clamped at MIN_SL ({MIN_SL_PIPS}p): {df['is_clamped'].sum()} ({df['is_clamped'].mean() * 100:.1f}%)")

    analyze_clamped_atr(df)
    sensitivity_test(df)
    analyze_tp_contribution(df)

    # Final summary
    print(f"{'=' * 100}")
    print(f"  KEY FINDINGS")
    print(f"{'=' * 100}")

    clamped = df[df['is_clamped']]
    unclamped = df[~df['is_clamped']]
    wr_c = clamped['win'].mean() * 100
    wr_u = unclamped['win'].mean() * 100

    print(f"  1. Clamped trades WR: {wr_c:.1f}% vs Unclamped: {wr_u:.1f}% (gap: {wr_u - wr_c:.1f}pp)")
    print(f"  2. Clamping happens when ATR < {MIN_SL_PIPS / SL_ATR_MULT:.0f}p (low volatility)")
    print(f"  3. Clamped SL is actually WIDER than ATR suggests (forced up to {MIN_SL_PIPS}p)")

    tp_winners = df[(df['win']) & (df['close_reason'].isin(['tp', 'tp_gap']))]
    total_winner_pnl = df[df['win']]['profit_usd'].sum()
    tp_pnl = tp_winners['profit_usd'].sum()
    tp_pct = tp_pnl / total_winner_pnl * 100 if total_winner_pnl > 0 else 0
    print(f"  4. TP hit winners: {len(tp_winners)} trades, ${tp_pnl:+.2f} ({tp_pct:.1f}% of winner P&L)")
    print()


if __name__ == "__main__":
    main()
