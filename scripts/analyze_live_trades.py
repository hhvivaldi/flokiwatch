"""
Live Trade Diagnostic Report - Population B
Population B = trades with open_time >= 2026-02-16 (dashboard cutoff)
Comprehensive analysis: per-trade metrics, MFE/MAE, pillar scores, aggregate stats.
"""

import sqlite3
import pandas as pd
from datetime import datetime, timedelta
from pathlib import Path
import os

# Constants
PIP = 0.10  # XAUUSD pip size


def get_session(dt: datetime) -> str:
    """Determine trading session from UTC hour."""
    hour = dt.hour
    if 0 <= hour < 8:
        return "Asian"
    elif 8 <= hour < 13:
        return "London"
    elif 13 <= hour < 21:
        return "NY"
    else:
        return "Asian"


def compute_mfe_mae(direction: str, open_price: float, open_time: datetime,
                    close_time: datetime, m5_df: pd.DataFrame) -> tuple:
    """
    Compute Max Favorable Excursion (MFE) and Max Adverse Excursion (MAE) in pips.
    Returns (mfe_pips, mae_pips).
    """
    # Filter M5 candles during trade
    mask = (m5_df['time'] >= open_time) & (m5_df['time'] <= close_time)
    candles = m5_df.loc[mask]
    
    if candles.empty:
        return (None, None)
    
    if direction == "BUY":
        # MFE = max high - open, MAE = open - min low
        max_high = candles['high'].max()
        min_low = candles['low'].min()
        mfe = (max_high - open_price) / PIP
        mae = (open_price - min_low) / PIP
    else:  # SELL
        # MFE = open - min low, MAE = max high - open
        max_high = candles['high'].max()
        min_low = candles['low'].min()
        mfe = (open_price - min_low) / PIP
        mae = (max_high - open_price) / PIP
    
    return (round(mfe, 1), round(mae, 1))


def infer_close_reason(close_reason_raw: str, close_price: float, sl: float, tp: float,
                       direction: str) -> str:
    """Infer close reason from raw text and price levels."""
    reason = close_reason_raw or ""
    reason_lower = reason.lower()
    
    # Check explicit reasons
    if "take profit" in reason_lower or "tp" in reason_lower:
        return "TP hit"
    if "stop loss" in reason_lower or "sl" in reason_lower:
        return "SL hit"
    if "trailing" in reason_lower:
        return "Trailing"
    if "breakeven" in reason_lower or "be" in reason_lower:
        return "Breakeven"
    if "max time" in reason_lower or "timeout" in reason_lower:
        return "Max time"
    if "manual" in reason_lower:
        return "Manual"
    if "expert" in reason_lower:
        return "EA/Manual"
    
    # Infer from price
    if direction == "BUY":
        if abs(close_price - tp) < 1:
            return "TP hit"
        if abs(close_price - sl) < 1:
            return "SL hit"
    else:
        if abs(close_price - tp) < 1:
            return "TP hit"
        if abs(close_price - sl) < 1:
            return "SL hit"
    
    return reason if reason else "Unknown"


def find_nearest_analysis(open_time: datetime, analyses_df: pd.DataFrame) -> dict:
    """Find the analysis record closest to (but before) trade open time."""
    if analyses_df.empty:
        return {}
    
    # Filter analyses before open_time
    mask = analyses_df['timestamp'] <= open_time
    prior = analyses_df.loc[mask]
    
    if prior.empty:
        # Take first analysis after
        return analyses_df.iloc[0].to_dict() if len(analyses_df) > 0 else {}
    
    # Get the most recent one
    return prior.iloc[-1].to_dict()


def main():
    os.chdir(Path(__file__).parent.parent)  # Ensure we're in project root
    
    conn = sqlite3.connect('data/history.db')
    
    # Load trades (Population B = open_time >= 2026-02-16, matching dashboard cutoff)
    trades_df = pd.read_sql_query(
        "SELECT * FROM trades WHERE close_time IS NOT NULL AND open_time >= '2026-02-16' ORDER BY id", conn
    )
    
    # Load analyses
    analyses_df = pd.read_sql_query(
        "SELECT * FROM analyses ORDER BY timestamp", conn
    )
    analyses_df['timestamp'] = pd.to_datetime(analyses_df['timestamp'])
    
    conn.close()
    
    # Load M5 data for MFE/MAE
    m5_path = Path('data/XAUUSD_M5.csv')
    if m5_path.exists():
        m5_df = pd.read_csv(m5_path)
        # Column is 'datetime', not 'time'
        m5_df['time'] = pd.to_datetime(m5_df['datetime'])
    else:
        m5_df = pd.DataFrame()
        print("WARNING: M5 data not found, MFE/MAE will be unavailable")
    
    # Parse trade times (handle mixed ISO formats with microseconds)
    trades_df['open_time_dt'] = pd.to_datetime(trades_df['open_time'], format='mixed')
    trades_df['close_time_dt'] = pd.to_datetime(trades_df['close_time'], format='mixed')
    
    # Build per-trade report
    report_lines = []
    report_lines.append("=" * 100)
    report_lines.append("LIVE TRADE DIAGNOSTIC REPORT - Population B (open_time >= 2026-02-16)")
    report_lines.append(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    report_lines.append("=" * 100)
    report_lines.append("")
    
    # Summary stats
    total = len(trades_df)
    wins = trades_df[trades_df['profit'] > 0]
    losses = trades_df[trades_df['profit'] < 0]
    total_pnl = trades_df['profit'].sum()
    gross_profit = wins['profit'].sum() if len(wins) > 0 else 0
    gross_loss = abs(losses['profit'].sum()) if len(losses) > 0 else 0
    pf = gross_profit / gross_loss if gross_loss > 0 else float('inf')
    wr = len(wins) / total * 100 if total > 0 else 0
    
    report_lines.append("SUMMARY")
    report_lines.append("-" * 50)
    report_lines.append(f"Total trades:     {total}")
    report_lines.append(f"Wins:             {len(wins)}")
    report_lines.append(f"Losses:           {len(losses)}")
    report_lines.append(f"Win Rate:         {wr:.1f}%")
    report_lines.append(f"Profit Factor:    {pf:.2f}")
    report_lines.append(f"Total P&L:        ${total_pnl:.2f}")
    report_lines.append(f"Gross Profit:     ${gross_profit:.2f}")
    report_lines.append(f"Gross Loss:       ${gross_loss:.2f}")
    report_lines.append("")
    
    # Per-trade details
    report_lines.append("=" * 100)
    report_lines.append("PER-TRADE ANALYSIS")
    report_lines.append("=" * 100)
    
    trade_records = []
    
    for _, trade in trades_df.iterrows():
        ticket = trade['ticket']
        direction = trade['direction']
        open_price = trade['open_price']
        close_price = trade['close_price']
        sl = trade['sl']
        tp = trade['tp']
        profit = trade['profit'] or 0
        open_time_dt = trade['open_time_dt']
        close_time_dt = trade['close_time_dt']
        close_reason_raw = trade['close_reason'] or ""
        
        # Compute pips
        if direction == "BUY":
            pips = (close_price - open_price) / PIP
        else:
            pips = (open_price - close_price) / PIP
        
        # Duration
        if pd.notna(open_time_dt) and pd.notna(close_time_dt):
            duration = close_time_dt - open_time_dt
            duration_str = str(duration).split('.')[0]  # Remove microseconds
        else:
            duration = None
            duration_str = "N/A"
        
        # Session
        session = get_session(open_time_dt) if pd.notna(open_time_dt) else "N/A"
        
        # Close reason
        close_reason = infer_close_reason(close_reason_raw, close_price, sl, tp, direction)
        
        # MFE/MAE
        if not m5_df.empty and pd.notna(open_time_dt) and pd.notna(close_time_dt):
            mfe, mae = compute_mfe_mae(direction, open_price, open_time_dt, close_time_dt, m5_df)
        else:
            mfe, mae = (None, None)
        
        # Breakeven/Trailing activation inference
        # Breakeven trigger = 70% of SL distance (not fixed +20 pips)
        # Trailing trigger = 100% of SL distance
        if direction == "BUY":
            sl_distance_pips = (open_price - sl) / PIP
        else:
            sl_distance_pips = (sl - open_price) / PIP
        
        be_threshold = sl_distance_pips * 0.70  # 70% of SL distance
        trailing_threshold = sl_distance_pips * 1.0  # 100% of SL distance
        
        be_activated = mfe is not None and mfe >= be_threshold
        trailing_activated = mfe is not None and mfe >= trailing_threshold
        
        # Find nearest analysis for pillar scores
        analysis = find_nearest_analysis(open_time_dt, analyses_df) if pd.notna(open_time_dt) else {}
        
        tech_score = analysis.get('tech_score', None)
        ml_score = analysis.get('ml_score', None)
        momentum_score = analysis.get('momentum_score', None)
        news_score = analysis.get('news_score', None)
        calendar_score = analysis.get('calendar_score', None)
        final_score = analysis.get('final_score', None)
        confidence = analysis.get('confidence', None)
        scenario = analysis.get('scenario', None)
        
        # Store record
        trade_records.append({
            'ticket': ticket,
            'direction': direction,
            'open_time': open_time_dt,
            'close_time': close_time_dt,
            'open_price': open_price,
            'close_price': close_price,
            'sl': sl,
            'tp': tp,
            'profit': profit,
            'pips': pips,
            'close_reason': close_reason,
            'scenario': scenario,
            'confidence': confidence,
            'tech_score': tech_score,
            'ml_score': ml_score,
            'momentum_score': momentum_score,
            'news_score': news_score,
            'calendar_score': calendar_score,
            'final_score': final_score,
            'session': session,
            'duration': duration,
            'duration_str': duration_str,
            'sl_distance_pips': sl_distance_pips,
            'be_threshold': be_threshold,
            'be_activated': be_activated,
            'trailing_activated': trailing_activated,
            'mfe': mfe,
            'mae': mae,
            'is_win': profit > 0,
        })
        
        # Format per-trade output
        report_lines.append("")
        report_lines.append(f"--- Trade #{ticket} ({direction}) ---")
        report_lines.append(f"Open:        {open_time_dt} | Close: {close_time_dt}")
        report_lines.append(f"Entry:       {open_price:.2f} | Exit: {close_price:.2f}")
        report_lines.append(f"SL:          {sl:.2f} | TP: {tp:.2f}")
        report_lines.append(f"P&L:         ${profit:+.2f} ({pips:+.1f} pips)")
        report_lines.append(f"Close:       {close_reason}")
        report_lines.append(f"Scenario:    {scenario}")
        report_lines.append(f"Confidence:  {confidence}")
        report_lines.append(f"Pillars:     Tech={tech_score} | ML={ml_score} | Mom={momentum_score} | News={news_score} | Cal={calendar_score}")
        report_lines.append(f"Brain Score: {final_score}")
        report_lines.append(f"Session:     {session}")
        report_lines.append(f"Duration:    {duration_str}" + (" [DATA ISSUE: open=close time]" if duration and duration.total_seconds() == 0 else ""))
        report_lines.append(f"SL Dist:     {sl_distance_pips:.1f} pips | BE Threshold: {be_threshold:.1f} pips (70% of SL)")
        report_lines.append(f"BE Active:   {'Yes' if be_activated else 'No'} | Trailing Active: {'Yes' if trailing_activated else 'No'}")
        report_lines.append(f"MFE:         {mfe} pips | MAE: {mae} pips")
    
    # Convert to DataFrame for aggregates
    records_df = pd.DataFrame(trade_records)
    
    # Aggregate analyses
    report_lines.append("")
    report_lines.append("=" * 100)
    report_lines.append("AGGREGATE ANALYSIS")
    report_lines.append("=" * 100)
    
    # 1. Wins vs Losses by Scenario
    report_lines.append("")
    report_lines.append("1. WINS VS LOSSES BY SCENARIO")
    report_lines.append("-" * 50)
    scenarios = records_df['scenario'].dropna().unique()
    for sc in scenarios:
        sc_trades = records_df[records_df['scenario'] == sc]
        sc_wins = sc_trades[sc_trades['is_win']]
        sc_losses = sc_trades[~sc_trades['is_win']]
        sc_pnl = sc_trades['profit'].sum()
        report_lines.append(f"  {sc}: {len(sc_wins)}W / {len(sc_losses)}L | P&L: ${sc_pnl:+.2f}")
    
    # 2. Wins vs Losses by Session
    report_lines.append("")
    report_lines.append("2. WINS VS LOSSES BY SESSION")
    report_lines.append("-" * 50)
    for sess in ["Asian", "London", "NY"]:
        sess_trades = records_df[records_df['session'] == sess]
        if len(sess_trades) == 0:
            continue
        sess_wins = sess_trades[sess_trades['is_win']]
        sess_losses = sess_trades[~sess_trades['is_win']]
        sess_pnl = sess_trades['profit'].sum()
        sess_wr = len(sess_wins) / len(sess_trades) * 100
        report_lines.append(f"  {sess}: {len(sess_wins)}W / {len(sess_losses)}L ({sess_wr:.0f}% WR) | P&L: ${sess_pnl:+.2f}")
    
    # 3. Wins vs Losses by Close Reason
    report_lines.append("")
    report_lines.append("3. WINS VS LOSSES BY CLOSE REASON")
    report_lines.append("-" * 50)
    for reason in records_df['close_reason'].unique():
        reason_trades = records_df[records_df['close_reason'] == reason]
        reason_wins = reason_trades[reason_trades['is_win']]
        reason_losses = reason_trades[~reason_trades['is_win']]
        reason_pnl = reason_trades['profit'].sum()
        report_lines.append(f"  {reason}: {len(reason_wins)}W / {len(reason_losses)}L | P&L: ${reason_pnl:+.2f}")
    
    # 4. Average Time in Trade
    report_lines.append("")
    report_lines.append("4. AVERAGE TIME IN TRADE")
    report_lines.append("-" * 50)
    wins_with_dur = records_df[(records_df['is_win']) & (records_df['duration'].notna())]
    losses_with_dur = records_df[(~records_df['is_win']) & (records_df['duration'].notna())]
    if len(wins_with_dur) > 0:
        avg_win_dur = wins_with_dur['duration'].mean()
        report_lines.append(f"  Avg duration (wins):   {str(avg_win_dur).split('.')[0]}")
    if len(losses_with_dur) > 0:
        avg_loss_dur = losses_with_dur['duration'].mean()
        report_lines.append(f"  Avg duration (losses): {str(avg_loss_dur).split('.')[0]}")
    
    # 5. Breakeven Activation Rate
    report_lines.append("")
    report_lines.append("5. BREAKEVEN ACTIVATION RATE")
    report_lines.append("-" * 50)
    avg_be_threshold = records_df['be_threshold'].mean() if 'be_threshold' in records_df.columns else 0
    be_count = records_df['be_activated'].sum()
    be_rate = be_count / len(records_df) * 100 if len(records_df) > 0 else 0
    report_lines.append(f"  BE threshold = 70% of SL distance (avg: {avg_be_threshold:.1f} pips)")
    report_lines.append(f"  Trades that reached BE level: {be_count}/{len(records_df)} ({be_rate:.0f}%)")
    
    # How many losses had BE activated but still lost?
    losses_with_be = records_df[(~records_df['is_win']) & (records_df['be_activated'])]
    report_lines.append(f"  Losses that reached BE first: {len(losses_with_be)}")
    
    # 6. MFE on Losses (CRITICAL)
    report_lines.append("")
    report_lines.append("6. MAX FAVORABLE EXCURSION ON LOSSES (CRITICAL)")
    report_lines.append("-" * 50)
    losses_df = records_df[~records_df['is_win']]
    losses_with_mfe = losses_df[losses_df['mfe'].notna()]
    
    if len(losses_with_mfe) > 0:
        avg_mfe_loss = losses_with_mfe['mfe'].mean()
        max_mfe_loss = losses_with_mfe['mfe'].max()
        report_lines.append(f"  Avg MFE on losses: {avg_mfe_loss:.1f} pips")
        report_lines.append(f"  Max MFE on losses: {max_mfe_loss:.1f} pips")
        
        # Count losses that went positive
        losses_went_positive = losses_with_mfe[losses_with_mfe['mfe'] > 0]
        losses_went_30plus = losses_with_mfe[losses_with_mfe['mfe'] >= 30]
        report_lines.append(f"  Losses that went positive first: {len(losses_went_positive)}/{len(losses_with_mfe)}")
        report_lines.append(f"  Losses that went +30 pips before reversing: {len(losses_went_30plus)}/{len(losses_with_mfe)}")
        
        report_lines.append("")
        report_lines.append("  Detail per losing trade:")
        for _, loss in losses_with_mfe.iterrows():
            report_lines.append(f"    #{loss['ticket']}: MFE={loss['mfe']:.1f} pips, MAE={loss['mae']:.1f} pips, P&L=${loss['profit']:.2f}")
    else:
        report_lines.append("  No MFE data available for losses")
    
    # Diagnosis
    report_lines.append("")
    report_lines.append("=" * 100)
    report_lines.append("DIAGNOSIS")
    report_lines.append("=" * 100)
    
    # Determine root cause
    if len(losses_with_mfe) > 0:
        avg_mfe = losses_with_mfe['mfe'].mean()
        losses_positive = len(losses_with_mfe[losses_with_mfe['mfe'] > 0])
        
        if avg_mfe >= 20 or losses_positive >= len(losses_with_mfe) * 0.5:
            report_lines.append("")
            report_lines.append(">>> EXIT MANAGEMENT PROBLEM <<<")
            report_lines.append("Losing trades went into profit before reversing to SL.")
            report_lines.append("Breakeven/trailing may be too slow or not activating properly.")
        else:
            report_lines.append("")
            report_lines.append(">>> ENTRY TIMING PROBLEM <<<")
            report_lines.append("Losing trades never went significantly positive.")
            report_lines.append("Entry signals may be firing at wrong times.")
    
    # Write report
    output_path = Path('data/live_trade_analysis.txt')
    with open(output_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(report_lines))
    
    print(f"Report saved to {output_path}")
    print('\n'.join(report_lines))


if __name__ == "__main__":
    main()
