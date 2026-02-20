"""
BACKTEST ENGINE — XAUUSD Trading Bot
=====================================
Replays historical H1 candles through the full Central Brain pipeline,
simulates trades with M5-precision SL/TP/trailing, and produces a detailed report.

Usage:
    python scripts/run_backtest.py

News = 50, Calendar = 50 (neutral) — tests Tech + ML + Momentum + M5 + Visual Features.
"""

import os
import sys
import json
import numpy as np
import pandas as pd
from datetime import datetime, timedelta
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
from copy import deepcopy

# Add parent dir to path
ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT_DIR)

import MetaTrader5 as mt5
import config
from technical_analyzer import calculate_indicators, calculate_technical_score, analyze_technical_detailed, get_atr_value, calculate_visual_features
from momentum_detector import analyze_momentum, calculate_adx, analyze_volume, count_consecutive_candles, analyze_atr_trend, detect_breakout, calculate_momentum_score
from central_brain import analyze_with_brain, is_actionable_signal, get_trade_direction, set_base_weights, reset_base_weights, _check_mtf_trend_alignment
from risk_manager import calculate_sl_tp
from ml_predictor import MLPredictor
from support_resistance import detect_zones_dual, get_sr_context, adjust_sl_tp_for_sr, is_near_strong_zone, format_zones_for_explanation


# ============================================================================
# CONFIG
# ============================================================================
MT5_ACCOUNT = config.MT5_ACCOUNT
MT5_PASSWORD = config.MT5_PASSWORD
MT5_SERVER = config.MT5_SERVER

# Backtest period (6 months for S/R validation)
BT_START = datetime(2025, 8, 18)
BT_END = datetime(2026, 2, 16, 12, 0)

# Warmup: need ~100 H1 candles before first trade for indicators
H1_WARMUP = 100

# SL/TP simulation
PIP_SIZE = 0.1  # XAU/USD: 1 pip = $0.1
PIP_VALUE_001 = 0.10  # $0.10 per pip for 0.01 lot

# Neutral pillars (news score overridable via CLI --news-score)
BT_NEWS_SCORE = 50.0  # Default; changed by --news-score arg

def _make_news_dict(score: float) -> dict:
    return {
        "score": score, "dxy": {"value": 104.0, "change_24h": 0.0, "trend": "stable"},
        "yields": {"value": 4.5, "change_24h": 0.0, "trend": "stable"},
        "vix": {"value": 17.0, "level": "low"},
        "sentiment": {"headlines_score": score, "normalized": 0},
        "high_impact_news_soon": False, "geopolitical_risk": "low",
        "anomalies": [],
    }

NEUTRAL_NEWS = _make_news_dict(BT_NEWS_SCORE)
NEUTRAL_CALENDAR = {
    "score": 50.0, "bias": "NEUTRAL", "phase": "normal",
    "phase_description": "Backtest — no calendar data",
    "events": [], "events_count": 0, "closest_event": None,
    "source": "backtest_neutral",
}


# ============================================================================
# DATA CLASSES
# ============================================================================

@dataclass
class SimTrade:
    ticket: int
    direction: str
    entry_price: float
    entry_time: datetime
    sl: float
    tp: float
    atr: float
    brain_score: float
    confidence: float
    scenario: str
    scenario_desc: str
    explanation_snippet: str
    # Filled on close
    close_price: float = 0.0
    close_time: Optional[datetime] = None
    close_reason: str = ""
    profit_pips: float = 0.0
    profit_usd: float = 0.0
    duration_minutes: float = 0.0
    max_favorable_pips: float = 0.0
    max_adverse_pips: float = 0.0
    is_pyramid: bool = False
    early_exit_reason: str = ""  # pyramid_protection_drawdown / pyramid_protection_combined / pyramid_protection_speed / extreme_market_exit
    # Pillar scores at entry
    tech_score: float = 50.0
    ml_score: float = 50.0
    momentum_score: float = 50.0
    confirmations: List[str] = field(default_factory=list)
    alerts: List[str] = field(default_factory=list)
    # Candlestick pattern at entry
    candlestick_pattern: str = ""
    candlestick_score: float = 0.0
    candlestick_sr_mult: float = 1.0


# ============================================================================
# BACKTEST ML PREDICTOR (offline — no live MT5 calls for H4/M5)
# ============================================================================

class BacktestMLPredictor(MLPredictor):
    """ML Predictor that uses historical DataFrames instead of live MT5."""

    def __init__(self, models_dir: str = None):
        super().__init__()
        if models_dir:
            self._models_dir = models_dir
        self._bt_h4_features = {'rsi_H4': 50.0, 'price_change_H4': 0.0, 'dist_ema21_H4': 0.0}
        self._bt_m5_features = {'volume_spike_M5': 0.0, 'price_change_M30': 0.0}

    def set_h4_features(self, feats: Dict):
        self._bt_h4_features = feats

    def set_m5_features(self, feats: Dict):
        self._bt_m5_features = feats

    def _get_h4_features(self) -> Dict:
        return self._bt_h4_features

    def _get_m5_features(self) -> Dict:
        return self._bt_m5_features

    def _get_xag_change_1h(self) -> float:
        return 0.0  # No silver data in backtest


# ============================================================================
# DATA COLLECTION
# ============================================================================

def connect():
    if not mt5.initialize():
        print(f"❌ MT5 init failed: {mt5.last_error()}")
        return False
    # Use whatever account is already logged in on the terminal
    info = mt5.account_info()
    if info:
        print(f"✅ MT5 connected: account {info.login} on {info.server}")
    else:
        print(f"✅ MT5 initialized (no account info available)")
    return True


def fetch_data(symbol: str, timeframe, start: datetime, end: datetime) -> pd.DataFrame:
    """Fetch OHLCV from MT5."""
    rates = mt5.copy_rates_range(symbol, timeframe, start, end)
    if rates is None or len(rates) == 0:
        print(f"  ❌ No data for {symbol} tf={timeframe}")
        return pd.DataFrame()
    df = pd.DataFrame(rates)
    df['datetime'] = pd.to_datetime(df['time'], unit='s')
    df = df.rename(columns={'tick_volume': 'volume'})
    return df


def fetch_data_chunked(symbol: str, timeframe, start: datetime, end: datetime, chunk_days: int = 60) -> pd.DataFrame:
    """Fetch OHLCV from MT5 in chunks (for large M5 requests that exceed MT5 per-request limits)."""
    all_dfs = []
    chunk_start = start
    while chunk_start < end:
        chunk_end = min(chunk_start + timedelta(days=chunk_days), end)
        rates = mt5.copy_rates_range(symbol, timeframe, chunk_start, chunk_end)
        if rates is not None and len(rates) > 0:
            chunk_df = pd.DataFrame(rates)
            chunk_df['datetime'] = pd.to_datetime(chunk_df['time'], unit='s')
            chunk_df = chunk_df.rename(columns={'tick_volume': 'volume'})
            all_dfs.append(chunk_df)
        chunk_start = chunk_end
    if not all_dfs:
        return pd.DataFrame()
    df = pd.concat(all_dfs, ignore_index=True).drop_duplicates(subset='time')
    df = df.sort_values('datetime').reset_index(drop=True)
    return df


def collect_all_data() -> Dict[str, pd.DataFrame]:
    """Collect M5, M15, H1, H4, D1 data for the backtest period."""
    # Extra buffer for warmup
    warmup_start = BT_START - timedelta(days=7)

    print("\n📊 Collecting data from MT5...")
    data = {}

    data['h1'] = fetch_data("XAUUSD", mt5.TIMEFRAME_H1, warmup_start, BT_END)
    print(f"  H1: {len(data['h1'])} candles")

    data['h4'] = fetch_data("XAUUSD", mt5.TIMEFRAME_H4, warmup_start - timedelta(days=30), BT_END)
    print(f"  H4: {len(data['h4'])} candles")

    # D1 for MTF trend check (need ~60 bars for EMA50)
    data['d1'] = fetch_data("XAUUSD", mt5.TIMEFRAME_D1, warmup_start - timedelta(days=90), BT_END)
    print(f"  D1: {len(data['d1'])} candles")

    # M5: use chunked fetch for long periods (MT5 limits ~35k candles per request)
    m5_start = BT_START - timedelta(days=2)
    data['m5'] = fetch_data_chunked("XAUUSD", mt5.TIMEFRAME_M5, m5_start, BT_END, chunk_days=60)
    print(f"  M5: {len(data['m5'])} candles (chunked)")

    data['m15'] = fetch_data("XAUUSD", mt5.TIMEFRAME_M15, BT_START - timedelta(days=2), BT_END)
    print(f"  M15: {len(data['m15'])} candles")

    return data


# ============================================================================
# OFFLINE HELPERS (replace live MT5 calls)
# ============================================================================

def compute_h4_features(df_h4: pd.DataFrame, h1_time: datetime) -> Dict:
    """Compute H4 features at a given H1 candle time."""
    mask = df_h4['datetime'] <= h1_time
    h4_slice = df_h4[mask]
    if len(h4_slice) < 22:
        return {'rsi_H4': 50.0, 'price_change_H4': 0.0, 'dist_ema21_H4': 0.0}

    h4_slice = h4_slice.tail(30).copy()

    # price_change_H4: 1-bar return %
    price_change = (h4_slice['close'].iloc[-1] / h4_slice['close'].iloc[-2] - 1) * 100

    # RSI H4
    delta = h4_slice['close'].diff()
    gain = delta.where(delta > 0, 0.0).rolling(14).mean()
    loss = (-delta.where(delta < 0, 0.0)).rolling(14).mean()
    rs = gain / loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    rsi_val = float(rsi.iloc[-1]) if not np.isnan(rsi.iloc[-1]) else 50.0

    # dist_ema21_H4
    ema21 = h4_slice['close'].ewm(span=21, adjust=False).mean()
    dist = (h4_slice['close'].iloc[-1] - ema21.iloc[-1]) / h4_slice['close'].iloc[-1] * 100

    return {
        'rsi_H4': rsi_val,
        'price_change_H4': float(price_change),
        'dist_ema21_H4': float(dist),
    }


def compute_m5_features(df_m5: pd.DataFrame, h1_time: datetime) -> Dict:
    """Compute M5 features (volume_spike, price_change_M30) at a given H1 time."""
    mask = df_m5['datetime'] <= h1_time
    m5_slice = df_m5[mask].tail(25)
    if len(m5_slice) < 20:
        return {'volume_spike_M5': 0.0, 'price_change_M30': 0.0}

    # volume_spike_M5
    vol_last3 = m5_slice['volume'].iloc[-3:].sum()
    vol_avg20 = m5_slice['volume'].iloc[-20:].mean()
    vol_spike = (vol_last3 / (3 * vol_avg20)) if vol_avg20 > 0 else 0.0

    # price_change_M30 (6 M5 candles)
    price_change_m30 = 0.0
    if len(m5_slice) >= 7:
        c_now = float(m5_slice['close'].iloc[-1])
        c_6ago = float(m5_slice['close'].iloc[-7])
        if c_6ago > 0:
            price_change_m30 = ((c_now - c_6ago) / c_6ago) * 100

    return {'volume_spike_M5': float(vol_spike), 'price_change_M30': float(price_change_m30)}


def compute_m5_status(df_m5: pd.DataFrame, h1_time: datetime, n_candles: int = 6) -> Dict:
    """Compute M5 status (for score adjustment) at a given H1 time."""
    mask = df_m5['datetime'] <= h1_time
    m5_slice = df_m5[mask].tail(n_candles + 1)

    if len(m5_slice) < n_candles:
        return {"move_pct": 0.0, "green_count": 0, "red_count": 0, "description": "M5 insufficient"}

    # Use only completed candles
    if len(m5_slice) > n_candles:
        m5_slice = m5_slice.iloc[:-1]

    open_price = float(m5_slice.iloc[0]['open'])
    close_price = float(m5_slice.iloc[-1]['close'])
    if open_price <= 0:
        return {"move_pct": 0.0, "green_count": 0, "red_count": 0, "description": "M5 error"}

    move_pct = ((close_price - open_price) / open_price) * 100
    green_count = sum(1 for _, r in m5_slice.iterrows() if r['close'] > r['open'])
    red_count = sum(1 for _, r in m5_slice.iterrows() if r['close'] < r['open'])

    return {
        "move_pct": round(move_pct, 4),
        "green_count": green_count,
        "red_count": red_count,
        "description": f"{move_pct:+.2f}% ({green_count}g/{red_count}r)",
    }


def compute_volatility_status(df_m5: pd.DataFrame, h1_time: datetime) -> Dict:
    """Offline volatility guard using historical M5 data."""
    mask = df_m5['datetime'] <= h1_time
    m5_slice = df_m5[mask].tail(20)

    if len(m5_slice) < 2:
        return {"status": "NORMAL", "last_extreme_candle": None,
                "minutes_since_extreme": None, "extreme_percent": 0,
                "cooling_reason": None, "description": "No M5 data"}

    threshold = config.EXTREME_CANDLE_THRESHOLD_PERCENT
    now = h1_time

    # Find most recent extreme candle
    last_extreme_idx = None
    for i in range(len(m5_slice) - 1, -1, -1):
        row = m5_slice.iloc[i]
        body_pct = abs(row['close'] - row['open']) / row['open'] * 100 if row['open'] > 0 else 0
        if body_pct >= threshold:
            last_extreme_idx = i
            break

    if last_extreme_idx is None:
        return {"status": "NORMAL", "last_extreme_candle": None,
                "minutes_since_extreme": None, "extreme_percent": 0,
                "cooling_reason": None, "description": "No extreme candles"}

    extreme_row = m5_slice.iloc[last_extreme_idx]
    extreme_time = extreme_row['datetime']
    if hasattr(extreme_time, 'to_pydatetime'):
        extreme_time = extreme_time.to_pydatetime()
    minutes_since = (now - extreme_time).total_seconds() / 60
    pct = abs(extreme_row['close'] - extreme_row['open']) / extreme_row['open'] * 100
    direction = "DOWN" if extreme_row['close'] < extreme_row['open'] else "UP"

    extreme_info = {"time": extreme_time, "move_percent": round(pct, 2),
                    "direction": direction, "minutes_ago": round(minutes_since, 1)}

    is_last = (last_extreme_idx == len(m5_slice) - 1)
    if is_last:
        return {"status": "EXTREME", "last_extreme_candle": extreme_info,
                "minutes_since_extreme": minutes_since, "extreme_percent": pct,
                "cooling_reason": None,
                "description": f"EXTREME: {direction} {pct:.2f}% ({minutes_since:.0f} min ago)"}

    next_row = m5_slice.iloc[last_extreme_idx + 1]
    next_body_pct = abs(next_row['close'] - next_row['open']) / next_row['open'] * 100 if next_row['open'] > 0 else 0
    next_dir = "DOWN" if next_row['close'] < next_row['open'] else "UP"
    same_dir = (next_dir == direction)

    if next_body_pct < config.EXTREME_CANCEL_THRESHOLD_PERCENT:
        return {"status": "NORMAL", "last_extreme_candle": None,
                "minutes_since_extreme": None, "extreme_percent": 0,
                "cooling_reason": None, "description": "Extreme cancelled (normalized)"}

    if not same_dir and next_body_pct >= config.EXTREME_CONFIRM_THRESHOLD_PERCENT:
        return {"status": "NORMAL", "last_extreme_candle": None,
                "minutes_since_extreme": None, "extreme_percent": 0,
                "cooling_reason": None, "description": "Extreme cancelled (strong reversal)"}

    if same_dir and next_body_pct >= config.EXTREME_CONFIRM_THRESHOLD_PERCENT:
        cooling_min = config.COOLING_CONFIRMED_MINUTES
        if minutes_since <= cooling_min:
            return {"status": "COOLING_DOWN", "last_extreme_candle": extreme_info,
                    "minutes_since_extreme": minutes_since, "extreme_percent": pct,
                    "cooling_reason": "confirmed",
                    "description": f"COOLING confirmed ({pct:.2f}% {minutes_since:.0f} min ago)"}
        return {"status": "NORMAL", "last_extreme_candle": None,
                "minutes_since_extreme": None, "extreme_percent": 0,
                "cooling_reason": None, "description": "Cascade expired"}

    cooling_min = config.COOLING_AMBIGUOUS_MINUTES
    if minutes_since <= cooling_min:
        return {"status": "COOLING_DOWN", "last_extreme_candle": extreme_info,
                "minutes_since_extreme": minutes_since, "extreme_percent": pct,
                "cooling_reason": "ambiguous",
                "description": f"COOLING ambiguous ({pct:.2f}% {minutes_since:.0f} min ago)"}

    return {"status": "NORMAL", "last_extreme_candle": None,
            "minutes_since_extreme": None, "extreme_percent": 0,
            "cooling_reason": None, "description": "Ambiguous extreme expired"}


def compute_m5_reversal(df_m5: pd.DataFrame, h1_time: datetime, direction: str) -> Dict:
    """Offline M5 reversal check."""
    n_candles = config.M5_REVERSAL_CANDLES
    moderate_threshold = config.M5_REVERSAL_MODERATE_THRESHOLD
    strong_threshold = config.M5_REVERSAL_STRONG_THRESHOLD

    mask = df_m5['datetime'] <= h1_time
    m5_slice = df_m5[mask].tail(n_candles + 1)

    if len(m5_slice) < n_candles:
        return {"reversal_detected": False, "reversal_strength": "none",
                "recent_move_pct": 0.0, "description": "M5 insufficient"}

    if len(m5_slice) > n_candles:
        m5_slice = m5_slice.iloc[:-1]

    open_price = float(m5_slice.iloc[0]['open'])
    close_price = float(m5_slice.iloc[-1]['close'])
    if open_price <= 0:
        return {"reversal_detected": False, "reversal_strength": "none",
                "recent_move_pct": 0.0, "description": "M5 error"}

    move_pct = ((close_price - open_price) / open_price) * 100
    direction = direction.upper()

    reversal_detected = False
    reversal_strength = "none"

    if direction == "SELL" and move_pct > 0:
        if move_pct >= strong_threshold:
            reversal_detected = True
            reversal_strength = "strong"
        elif move_pct >= moderate_threshold:
            reversal_detected = True
            reversal_strength = "moderate"
    elif direction == "BUY" and move_pct < 0:
        abs_move = abs(move_pct)
        if abs_move >= strong_threshold:
            reversal_detected = True
            reversal_strength = "strong"
        elif abs_move >= moderate_threshold:
            reversal_detected = True
            reversal_strength = "moderate"

    desc = f"M5 {'reversal ' + reversal_strength if reversal_detected else 'OK'}: {move_pct:+.2f}%"
    return {"reversal_detected": reversal_detected, "reversal_strength": reversal_strength,
            "recent_move_pct": round(move_pct, 4), "description": desc}


def compute_mtf_trend(df_d1: pd.DataFrame, df_h4: pd.DataFrame, h1_time: datetime,
                      ema_period: int = 50) -> Tuple[Optional[str], Optional[str]]:
    """
    Compute D1 and H4 trend direction at a given H1 candle time using EMA.
    
    Args:
        df_d1: D1 DataFrame with OHLCV
        df_h4: H4 DataFrame with OHLCV
        h1_time: Current H1 candle time
        ema_period: EMA period for trend detection (default 50)
    
    Returns:
        Tuple: (d1_trend, h4_trend) - each is "bullish", "bearish", or None
    """
    def get_trend(df: pd.DataFrame, ref_time: datetime) -> Optional[str]:
        if df is None or len(df) == 0:
            return None
        mask = df['datetime'] <= ref_time
        df_slice = df[mask]
        if len(df_slice) < ema_period:
            return None
        df_slice = df_slice.tail(ema_period + 10).copy()
        df_slice['ema'] = df_slice['close'].ewm(span=ema_period, adjust=False).mean()
        current_price = float(df_slice['close'].iloc[-1])
        current_ema = float(df_slice['ema'].iloc[-1])
        if current_price > current_ema:
            return "bullish"
        elif current_price < current_ema:
            return "bearish"
        return None
    
    d1_trend = get_trend(df_d1, h1_time)
    h4_trend = get_trend(df_h4, h1_time)
    
    return d1_trend, h4_trend


# ============================================================================
# TRADE SIMULATION (M5 precision)
# ============================================================================

def simulate_trade(trade: SimTrade, df_m5: pd.DataFrame, debug_ticket: int = 0) -> SimTrade:
    """
    Simulate SL/TP/breakeven/trailing using M5 candles after entry.
    Gap handling: if M5 opens beyond SL, close at M5 open price.
    """
    debug = (trade.ticket == debug_ticket)
    mask = df_m5['datetime'] > trade.entry_time
    m5_after = df_m5[mask].copy()

    if len(m5_after) == 0:
        trade.close_reason = "end_of_data"
        trade.close_price = trade.entry_price
        trade.close_time = trade.entry_time
        return trade

    current_sl = trade.sl
    current_tp = trade.tp
    breakeven_hit = False
    max_favorable = 0.0
    max_adverse = 0.0

    # Dynamic trailing based on actual SL distance (already capped by MIN/MAX_SL_PIPS)
    # This ensures trailing triggers are proportional to real risk, not raw ATR
    sl_pips = abs(trade.entry_price - trade.sl) / PIP_SIZE
    be_trigger = sl_pips * getattr(config, 'BREAKEVEN_ATR_MULT', 0.7)
    trail_trigger = sl_pips * getattr(config, 'TRAILING_ATR_MULT', 1.0)
    trail_distance = sl_pips * getattr(config, 'TRAILING_DISTANCE_ATR_MULT', 0.7)

    # Max duration check
    max_duration_min = getattr(config, 'MAX_POSITION_HOURS', 24) * 60
    min_profit_pips = getattr(config, 'MAX_POSITION_MIN_PROFIT_PIPS', 5)

    if debug:
        print(f"\n  DEBUG Trade #{trade.ticket} {trade.direction} @ {trade.entry_price:.2f}")
        print(f"     SL={trade.sl:.2f} TP={trade.tp:.2f} ATR={trade.atr:.2f} SL_pips={sl_pips:.0f}")
        print(f"     BE trigger={be_trigger:.0f} pips, Trail trigger={trail_trigger:.0f} pips, Trail dist={trail_distance:.0f} pips")
        print(f"     M5 candles after entry: {len(m5_after)}")

    for _, candle in m5_after.iterrows():
        c_open = float(candle['open'])
        c_high = float(candle['high'])
        c_low = float(candle['low'])
        c_close = float(candle['close'])
        c_time = candle['datetime']
        if hasattr(c_time, 'to_pydatetime'):
            c_time = c_time.to_pydatetime()

        if trade.direction == "BUY":
            # Gap check: M5 opens below SL
            if c_open <= current_sl:
                trade.close_price = c_open
                trade.close_time = c_time
                trade.close_reason = "sl_gap"
                break

            # Gap check: M5 opens above TP
            if c_open >= current_tp:
                trade.close_price = c_open
                trade.close_time = c_time
                trade.close_reason = "tp_gap"
                break

            # SL hit (conservative: check SL before TP if both in same candle)
            if c_low <= current_sl:
                trade.close_price = current_sl
                trade.close_time = c_time
                trade.close_reason = "sl"
                break

            # TP hit
            if c_high >= current_tp:
                trade.close_price = current_tp
                trade.close_time = c_time
                trade.close_reason = "tp"
                break

            # Track excursions
            favorable = (c_high - trade.entry_price) / PIP_SIZE
            adverse = (trade.entry_price - c_low) / PIP_SIZE
            max_favorable = max(max_favorable, favorable)
            max_adverse = max(max_adverse, adverse)

            # Max duration check
            elapsed_min = (c_time - trade.entry_time).total_seconds() / 60
            if elapsed_min >= max_duration_min:
                cur_profit = (c_close - trade.entry_price) / PIP_SIZE
                if cur_profit < min_profit_pips:
                    trade.close_price = c_close
                    trade.close_time = c_time
                    trade.close_reason = "max_duration"
                    if debug:
                        print(f"     ⏰ MAX DURATION {elapsed_min:.0f}min, profit={cur_profit:.1f} pips → close")
                    break

            # Breakeven
            if not breakeven_hit and favorable >= be_trigger:
                breakeven_hit = True
                current_sl = trade.entry_price + 0.2  # +2 pips spread
                if debug:
                    print(f"     ✅ BREAKEVEN @ {c_time} fav={favorable:.0f} pips, SL moved to {current_sl:.2f}")
                
            # Trailing
            if breakeven_hit and favorable >= trail_trigger:
                new_sl = c_high - trail_distance * PIP_SIZE
                if new_sl > current_sl:
                    if debug:
                        print(f"     📈 TRAILING @ {c_time} fav={favorable:.0f} pips, SL {current_sl:.2f} → {new_sl:.2f}")
                    current_sl = new_sl

            if debug and favorable > 300:
                print(f"     M5 {c_time} O={c_open:.2f} H={c_high:.2f} L={c_low:.2f} C={c_close:.2f} fav={favorable:.0f} adv={adverse:.0f} SL={current_sl:.2f} BE={breakeven_hit}")

        else:  # SELL
            # Gap check: M5 opens above SL
            if c_open >= current_sl:
                trade.close_price = c_open
                trade.close_time = c_time
                trade.close_reason = "sl_gap"
                break

            # Gap check: M5 opens below TP
            if c_open <= current_tp:
                trade.close_price = c_open
                trade.close_time = c_time
                trade.close_reason = "tp_gap"
                break

            # SL hit
            if c_high >= current_sl:
                trade.close_price = current_sl
                trade.close_time = c_time
                trade.close_reason = "sl"
                break

            # TP hit
            if c_low <= current_tp:
                trade.close_price = current_tp
                trade.close_time = c_time
                trade.close_reason = "tp"
                break

            # Track excursions
            favorable = (trade.entry_price - c_low) / PIP_SIZE
            adverse = (c_high - trade.entry_price) / PIP_SIZE
            max_favorable = max(max_favorable, favorable)
            max_adverse = max(max_adverse, adverse)

            # Max duration check
            elapsed_min = (c_time - trade.entry_time).total_seconds() / 60
            if elapsed_min >= max_duration_min:
                cur_profit = (trade.entry_price - c_close) / PIP_SIZE
                if cur_profit < min_profit_pips:
                    trade.close_price = c_close
                    trade.close_time = c_time
                    trade.close_reason = "max_duration"
                    break

            # Breakeven
            if not breakeven_hit and favorable >= be_trigger:
                breakeven_hit = True
                current_sl = trade.entry_price - 0.2  # -2 pips spread

            # Trailing
            if breakeven_hit and favorable >= trail_trigger:
                new_sl = c_low + trail_distance * PIP_SIZE
                if new_sl < current_sl:
                    current_sl = new_sl

    else:
        # End of data without SL/TP
        trade.close_price = float(m5_after.iloc[-1]['close'])
        trade.close_time = m5_after.iloc[-1]['datetime']
        if hasattr(trade.close_time, 'to_pydatetime'):
            trade.close_time = trade.close_time.to_pydatetime()
        trade.close_reason = "end_of_data"

    # Calculate P&L
    if trade.direction == "BUY":
        trade.profit_pips = (trade.close_price - trade.entry_price) / PIP_SIZE
    else:
        trade.profit_pips = (trade.entry_price - trade.close_price) / PIP_SIZE

    trade.profit_usd = trade.profit_pips * PIP_VALUE_001  # 0.01 lot
    trade.max_favorable_pips = round(max_favorable, 1)
    trade.max_adverse_pips = round(max_adverse, 1)

    if trade.close_time and trade.entry_time:
        trade.duration_minutes = (trade.close_time - trade.entry_time).total_seconds() / 60

    return trade


# ============================================================================
# CONCURRENT TRADE SIMULATION (for Early Exit awareness)
# ============================================================================

def _compute_profit_pips(trade: SimTrade, current_price: float) -> float:
    """Calculate current profit in pips for an open trade."""
    if trade.direction == "BUY":
        return (current_price - trade.entry_price) / PIP_SIZE
    else:
        return (trade.entry_price - current_price) / PIP_SIZE


def _check_pyramid_protection(trade: SimTrade, same_dir_open: List[SimTrade],
                               current_price: float, candle_time: datetime) -> Optional[str]:
    """
    Check if the newest pyramid trade should be early-exited.
    Only called on the newest trade in a same-direction group.
    
    Returns: early_exit_reason string or None
    """
    # Trigger 1: Individual drawdown of newest
    profit_pips = _compute_profit_pips(trade, current_price)
    if profit_pips <= -config.PYRAMID_EXIT_DRAWDOWN_PIPS:
        return "pyramid_protection_drawdown"

    # Trigger 2: Combined drawdown of all same-direction positions
    total_pnl = sum(_compute_profit_pips(t, current_price) * PIP_SIZE for t in same_dir_open)
    total_pnl += profit_pips * PIP_SIZE  # add current trade
    total_entry = sum(t.entry_price for t in same_dir_open) + trade.entry_price
    if total_entry > 0:
        combined_pct = (total_pnl / total_entry) * 100
        if combined_pct <= config.PYRAMID_EXIT_COMBINED_DRAWDOWN_PCT:
            return "pyramid_protection_combined"

    # Trigger 3: Speed — loses too many pips too fast
    elapsed_min = (candle_time - trade.entry_time).total_seconds() / 60
    if elapsed_min <= config.PYRAMID_EXIT_SPEED_MINUTES and profit_pips <= -config.PYRAMID_EXIT_SPEED_PIPS:
        return "pyramid_protection_speed"

    return None


def _check_extreme_exit(trade: SimTrade, vol_status: Dict, current_price: float,
                         grace_counter: Dict) -> Optional[str]:
    """
    Check if a position against an extreme spike should be early-exited.
    Uses a grace counter to wait EXTREME_EXIT_GRACE_CANDLES after detection.
    
    Returns: early_exit_reason string or None
    """
    if not config.EXTREME_EXIT_ENABLED:
        return None

    if vol_status.get('status') != 'EXTREME':
        # Reset grace counter when not EXTREME
        grace_counter.pop(trade.ticket, None)
        return None

    extreme_info = vol_status.get('last_extreme_candle')
    if not extreme_info:
        return None

    extreme_direction = extreme_info.get('direction', '')  # "UP" or "DOWN"

    # Check if position is against the spike
    position_against = (
        (trade.direction == "BUY" and extreme_direction == "DOWN") or
        (trade.direction == "SELL" and extreme_direction == "UP")
    )

    if not position_against:
        return None

    # Check minimum loss
    profit_pips = _compute_profit_pips(trade, current_price)
    if profit_pips > -config.EXTREME_EXIT_MIN_LOSS_PIPS:
        return None

    # Grace candle logic
    if trade.ticket not in grace_counter:
        grace_counter[trade.ticket] = 0

    grace_counter[trade.ticket] += 1

    if grace_counter[trade.ticket] > config.EXTREME_EXIT_GRACE_CANDLES:
        return "extreme_market_exit"

    return None


def simulate_trades_concurrent(trades_to_sim: List[SimTrade], df_m5: pd.DataFrame,
                                early_exit_enabled: bool = False) -> List[SimTrade]:
    """
    Simulate multiple trades concurrently through M5 candles.
    When early_exit_enabled, trades can see each other for pyramid protection
    and react to extreme volatility events.
    
    Each trade maintains its own SL/TP/trailing state.
    Early exit closes a trade before its SL/TP would normally be hit.
    """
    if not trades_to_sim:
        return []

    # Sort trades by entry time
    trades_to_sim = sorted(trades_to_sim, key=lambda t: t.entry_time)

    # Per-trade state
    state = {}
    for t in trades_to_sim:
        sl_pips = abs(t.entry_price - t.sl) / PIP_SIZE
        state[t.ticket] = {
            'current_sl': t.sl,
            'current_tp': t.tp,
            'breakeven_hit': False,
            'max_favorable': 0.0,
            'max_adverse': 0.0,
            'be_trigger': sl_pips * getattr(config, 'BREAKEVEN_ATR_MULT', 0.7),
            'trail_trigger': sl_pips * getattr(config, 'TRAILING_ATR_MULT', 0.7),
            'trail_distance': sl_pips * getattr(config, 'TRAILING_DISTANCE_ATR_MULT', 0.7),
            'closed': False,
        }

    # Grace counters for extreme exit (ticket → candle count since first EXTREME detection)
    grace_counters = {}

    # Max duration
    max_duration_min = getattr(config, 'MAX_POSITION_HOURS', 24) * 60
    min_profit_pips = getattr(config, 'MAX_POSITION_MIN_PROFIT_PIPS', 5)

    # Find earliest entry time to start M5 iteration
    earliest_entry = min(t.entry_time for t in trades_to_sim)
    mask = df_m5['datetime'] > earliest_entry
    m5_after = df_m5[mask]

    if len(m5_after) == 0:
        # No M5 data after entries — close all at entry
        for t in trades_to_sim:
            t.close_reason = "end_of_data"
            t.close_price = t.entry_price
            t.close_time = t.entry_time
        return trades_to_sim

    # Iterate through M5 candles
    for _, candle in m5_after.iterrows():
        c_open = float(candle['open'])
        c_high = float(candle['high'])
        c_low = float(candle['low'])
        c_close = float(candle['close'])
        c_time = candle['datetime']
        if hasattr(c_time, 'to_pydatetime'):
            c_time = c_time.to_pydatetime()

        # Check if all trades are closed
        open_trades = [t for t in trades_to_sim if not state[t.ticket]['closed']]
        if not open_trades:
            break

        # Compute volatility status at this candle (for extreme exit)
        vol_status_cache = None

        for trade in open_trades:
            # Skip if trade hasn't entered yet
            if c_time <= trade.entry_time:
                continue

            s = state[trade.ticket]
            current_sl = s['current_sl']
            current_tp = s['current_tp']

            # === NORMAL SL/TP/TRAILING LOGIC (same as simulate_trade) ===
            if trade.direction == "BUY":
                # Gap: M5 opens below SL
                if c_open <= current_sl:
                    trade.close_price = c_open
                    trade.close_time = c_time
                    trade.close_reason = "sl_gap"
                    s['closed'] = True
                    continue
                # Gap: M5 opens above TP
                if c_open >= current_tp:
                    trade.close_price = c_open
                    trade.close_time = c_time
                    trade.close_reason = "tp_gap"
                    s['closed'] = True
                    continue
                # SL hit
                if c_low <= current_sl:
                    trade.close_price = current_sl
                    trade.close_time = c_time
                    trade.close_reason = "sl"
                    s['closed'] = True
                    continue
                # TP hit
                if c_high >= current_tp:
                    trade.close_price = current_tp
                    trade.close_time = c_time
                    trade.close_reason = "tp"
                    s['closed'] = True
                    continue

                # Track excursions
                favorable = (c_high - trade.entry_price) / PIP_SIZE
                adverse = (trade.entry_price - c_low) / PIP_SIZE
                s['max_favorable'] = max(s['max_favorable'], favorable)
                s['max_adverse'] = max(s['max_adverse'], adverse)

                # Max duration
                elapsed_min = (c_time - trade.entry_time).total_seconds() / 60
                if elapsed_min >= max_duration_min:
                    cur_profit = (c_close - trade.entry_price) / PIP_SIZE
                    if cur_profit < min_profit_pips:
                        trade.close_price = c_close
                        trade.close_time = c_time
                        trade.close_reason = "max_duration"
                        s['closed'] = True
                        continue

                # Breakeven
                if not s['breakeven_hit'] and favorable >= s['be_trigger']:
                    s['breakeven_hit'] = True
                    s['current_sl'] = trade.entry_price + 0.2

                # Trailing
                if s['breakeven_hit'] and favorable >= s['trail_trigger']:
                    new_sl = c_high - s['trail_distance'] * PIP_SIZE
                    if new_sl > s['current_sl']:
                        s['current_sl'] = new_sl

            else:  # SELL
                # Gap: M5 opens above SL
                if c_open >= current_sl:
                    trade.close_price = c_open
                    trade.close_time = c_time
                    trade.close_reason = "sl_gap"
                    s['closed'] = True
                    continue
                # Gap: M5 opens below TP
                if c_open <= current_tp:
                    trade.close_price = c_open
                    trade.close_time = c_time
                    trade.close_reason = "tp_gap"
                    s['closed'] = True
                    continue
                # SL hit
                if c_high >= current_sl:
                    trade.close_price = current_sl
                    trade.close_time = c_time
                    trade.close_reason = "sl"
                    s['closed'] = True
                    continue
                # TP hit
                if c_low <= current_tp:
                    trade.close_price = current_tp
                    trade.close_time = c_time
                    trade.close_reason = "tp"
                    s['closed'] = True
                    continue

                # Track excursions
                favorable = (trade.entry_price - c_low) / PIP_SIZE
                adverse = (c_high - trade.entry_price) / PIP_SIZE
                s['max_favorable'] = max(s['max_favorable'], favorable)
                s['max_adverse'] = max(s['max_adverse'], adverse)

                # Max duration
                elapsed_min = (c_time - trade.entry_time).total_seconds() / 60
                if elapsed_min >= max_duration_min:
                    cur_profit = (trade.entry_price - c_close) / PIP_SIZE
                    if cur_profit < min_profit_pips:
                        trade.close_price = c_close
                        trade.close_time = c_time
                        trade.close_reason = "max_duration"
                        s['closed'] = True
                        continue

                # Breakeven
                if not s['breakeven_hit'] and favorable >= s['be_trigger']:
                    s['breakeven_hit'] = True
                    s['current_sl'] = trade.entry_price - 0.2

                # Trailing
                if s['breakeven_hit'] and favorable >= s['trail_trigger']:
                    new_sl = c_low + s['trail_distance'] * PIP_SIZE
                    if new_sl < s['current_sl']:
                        s['current_sl'] = new_sl

            # === EARLY EXIT CHECKS (only if enabled and trade still open) ===
            if early_exit_enabled and not s['closed']:
                # --- Pyramid Protection ---
                # Find other open trades in same direction
                same_dir_open = [
                    t for t in open_trades
                    if t.direction == trade.direction
                    and t.ticket != trade.ticket
                    and not state[t.ticket]['closed']
                    and t.entry_time < c_time  # must have entered
                ]

                if same_dir_open and trade.is_pyramid:
                    # Only check on the newest trade in the group
                    all_same_dir = same_dir_open + [trade]
                    newest = max(all_same_dir, key=lambda t: t.entry_time)
                    if newest.ticket == trade.ticket:
                        reason = _check_pyramid_protection(trade, same_dir_open, c_close, c_time)
                        if reason:
                            trade.close_price = c_close
                            trade.close_time = c_time
                            trade.close_reason = "early_exit"
                            trade.early_exit_reason = reason
                            s['closed'] = True
                            continue

                # --- Extreme Market Exit ---
                if vol_status_cache is None:
                    vol_status_cache = compute_volatility_status(df_m5, c_time)

                reason = _check_extreme_exit(trade, vol_status_cache, c_close, grace_counters)
                if reason:
                    trade.close_price = c_close
                    trade.close_time = c_time
                    trade.close_reason = "early_exit"
                    trade.early_exit_reason = reason
                    s['closed'] = True
                    continue

    # Close any remaining open trades at end of data
    for trade in trades_to_sim:
        s = state[trade.ticket]
        if not s['closed']:
            last_candle = m5_after.iloc[-1] if len(m5_after) > 0 else None
            if last_candle is not None:
                trade.close_price = float(last_candle['close'])
                trade.close_time = last_candle['datetime']
                if hasattr(trade.close_time, 'to_pydatetime'):
                    trade.close_time = trade.close_time.to_pydatetime()
            else:
                trade.close_price = trade.entry_price
                trade.close_time = trade.entry_time
            trade.close_reason = "end_of_data"
            s['closed'] = True

    # Finalize all trades (P&L, excursions, duration)
    for trade in trades_to_sim:
        s = state[trade.ticket]
        if trade.direction == "BUY":
            trade.profit_pips = (trade.close_price - trade.entry_price) / PIP_SIZE
        else:
            trade.profit_pips = (trade.entry_price - trade.close_price) / PIP_SIZE

        trade.profit_usd = trade.profit_pips * PIP_VALUE_001
        trade.max_favorable_pips = round(s['max_favorable'], 1)
        trade.max_adverse_pips = round(s['max_adverse'], 1)

        if trade.close_time and trade.entry_time:
            trade.duration_minutes = (trade.close_time - trade.entry_time).total_seconds() / 60

    return trades_to_sim


# ============================================================================
# MAIN BACKTEST LOOP
# ============================================================================

def run_backtest(data: Dict[str, pd.DataFrame], disable_visual: bool = False, disable_pyramid: bool = False,
                 early_exit: bool = False, sr_enabled: bool = False,
                 sr_tp_adjust: bool = True, models_dir: str = None,
                 quiet: bool = False, bt_predictor=None) -> Tuple[List[SimTrade], Dict]:
    """
    Run the backtest over H1 candles.
    
    Args:
        data: Dict with 'h1', 'h4', 'm5', 'm15' DataFrames
        disable_visual: If True, visual features return neutral (for comparison)
        disable_pyramid: If True, block ALL 2nd positions in same direction (pyramid OFF)
        early_exit: If True, enable Early Exit (pyramid protection + extreme market exit)
        sr_enabled: If True, compute S/R zones and pass to brain (confidence adj + scenario)
        sr_tp_adjust: If True AND sr_enabled, also adjust SL/TP based on S/R zones
        quiet: If True, suppress progress output (for optimizer)
        bt_predictor: Pre-loaded BacktestMLPredictor (avoids reloading models each run)
    
    Returns:
        Tuple of (trades list, pyramid_stats dict)
    """
    df_h1 = data['h1'].copy()
    df_h4 = data['h4'].copy()
    df_d1 = data.get('d1', pd.DataFrame()).copy()  # D1 for MTF trend check
    df_m5 = data['m5'].copy()

    # Initialize ML predictor (offline) — reuse if provided
    if bt_predictor is None:
        bt_predictor = BacktestMLPredictor(models_dir=models_dir)
        if not bt_predictor.load_model():
            print("❌ Failed to load ML models")
            return [], {'attempts': 0, 'blocked_profit': 0, 'blocked_max_pos': 0, 'allowed': 0, 'blocked_details': []}

    trades: List[SimTrade] = []
    open_trades: List[SimTrade] = []
    ticket_counter = 1000000

    # Safety state
    last_trade_time = {'BUY': None, 'SELL': None}
    last_close_type = {'BUY': None, 'SELL': None}

    # Pyramid instrumentation
    pyramid_stats = {
        'attempts': 0,
        'blocked_profit': 0,
        'blocked_max_pos': 0,
        'allowed': 0,
        'blocked_details': [],
    }

    # Find H1 candles in backtest range
    bt_mask = (df_h1['datetime'] >= BT_START) & (df_h1['datetime'] <= BT_END)
    bt_indices = df_h1[bt_mask].index.tolist()

    if not bt_indices:
        if not quiet:
            print("❌ No H1 candles in backtest range")
        return [], pyramid_stats

    first_bt_idx = bt_indices[0]
    total_candles = len(bt_indices)
    if not quiet:
        print(f"\n🔄 Running backtest: {total_candles} H1 candles ({BT_START} → {BT_END})")
        if disable_visual:
            print("   ⚠️ Visual features DISABLED (comparison mode)")

    decisions_log = []

    for count, idx in enumerate(bt_indices):
        if idx < H1_WARMUP:
            continue

        # Current H1 candle
        h1_candle = df_h1.iloc[idx]
        h1_time = h1_candle['datetime']
        if hasattr(h1_time, 'to_pydatetime'):
            h1_time = h1_time.to_pydatetime()

        current_price = float(h1_candle['close'])

        # Progress
        if not quiet and count % 50 == 0:
            pct = count / total_candles * 100
            print(f"   {pct:.0f}% — {h1_time.strftime('%Y-%m-%d %H:%M')} — {len(trades)} trades so far")

        # ============================================================
        # Close any open trades that should have closed by now
        # (trades are simulated immediately, but we track for pyramid)
        # ============================================================
        still_open = []
        for t in open_trades:
            if t.close_time and t.close_time <= h1_time:
                # Already closed
                if t.profit_pips >= 0:
                    last_close_type[t.direction] = "tp" if "tp" in t.close_reason else "trailing"
                else:
                    last_close_type[t.direction] = "sl"
                last_trade_time[t.direction] = t.close_time
            else:
                still_open.append(t)
        open_trades = still_open

        # ============================================================
        # Build H1 slice with indicators
        # ============================================================
        h1_slice = df_h1.iloc[:idx + 1].copy()
        h1_slice = calculate_indicators(h1_slice)

        if len(h1_slice) < 50:
            continue

        # ============================================================
        # Monkey-patch visual features if disabled
        # ============================================================
        if disable_visual:
            import technical_analyzer as _ta
            _orig_visual = _ta.calculate_visual_features
            _ta.calculate_visual_features = lambda df, sr_zones=None, current_price=None, atr=None: {
                "consecutive_candles": 0, "body_size_trend": "neutral",
                "price_vs_range": 0.5, "engulfing": None, "pin_bar": None,
                "candlestick_patterns": None,
                "adjustments": {}, "total_adjustment": 0.0,
            }

        # ============================================================
        # PILLAR 1: Technical
        # ============================================================
        tech_data = analyze_technical_detailed(h1_slice)

        # Restore visual features
        if disable_visual:
            _ta.calculate_visual_features = _orig_visual

        # ============================================================
        # PILLAR 2: ML (offline)
        # ============================================================
        h4_feats = compute_h4_features(df_h4, h1_time)
        m5_feats = compute_m5_features(df_m5, h1_time)
        bt_predictor.set_h4_features(h4_feats)
        bt_predictor.set_m5_features(m5_feats)

        try:
            ml_result = bt_predictor.predict(h1_slice, NEUTRAL_NEWS)
            ml_data = {
                "score": float(ml_result['score']),
                "score_h1": float(ml_result.get('score_h1', ml_result['score'])),
                "score_h4": float(ml_result.get('score_h4', ml_result['score'])),
                "prediction": "bullish" if ml_result['direction'] == 'BUY' else ("bearish" if ml_result['direction'] == 'SELL' else "neutral"),
                "probability": float(ml_result.get('raw_proba', ml_result['probability'])),
                "max_confidence": float(ml_result.get('max_confidence', 0.5)),
                "pattern": "undefined",
                "similar_patterns_count": None,
                "historical_success_rate": None,
                "error": ml_result.get('error'),
            }
            # Infer pattern
            if ml_data['max_confidence'] > 0.65:
                price_above_ema9 = current_price > float(h1_slice['ema_9'].iloc[-1])
                if ml_data['prediction'] == "bullish":
                    ml_data['pattern'] = "continuation" if price_above_ema9 else "reversal"
                elif ml_data['prediction'] == "bearish":
                    ml_data['pattern'] = "continuation" if not price_above_ema9 else "reversal"
            elif ml_data['max_confidence'] > 0.60:
                ml_data['pattern'] = "breakout"
        except Exception as e:
            ml_data = {"score": 50.0, "prediction": "neutral", "probability": 0.5,
                       "max_confidence": 0.5, "pattern": "undefined",
                       "similar_patterns_count": None, "historical_success_rate": None,
                       "error": str(e)}

        # ============================================================
        # PILLAR 3: Momentum
        # ============================================================
        momentum_data = analyze_momentum(h1_slice)

        # ============================================================
        # M5 Status (for score adjustment)
        # ============================================================
        m5_status = compute_m5_status(df_m5, h1_time)

        # ============================================================
        # Volatility Guard (offline)
        # ============================================================
        vol_status = compute_volatility_status(df_m5, h1_time)

        # ============================================================
        # Support & Resistance (offline)
        # ============================================================
        sr_brain_data = None
        sr_zones = []
        if sr_enabled:
            h1_for_sr = df_h1.iloc[:idx + 1].copy()
            h4_mask = df_h4['datetime'] <= h1_time
            h4_for_sr = df_h4[h4_mask].copy()

            sr_zones = detect_zones_dual(
                h1_for_sr, h4_for_sr,
                merge_pips=config.SR_ZONE_MERGE_PIPS,
                max_age_bars=config.SR_ZONE_MAX_AGE_BARS,
                min_touches=config.SR_MIN_TOUCHES,
            )

            if sr_zones:
                atr_for_sr = get_atr_value(h1_slice)
                # We don't know direction yet — compute context for both, apply after decision
                sr_context = get_sr_context(
                    sr_zones, current_price, atr_for_sr, direction=None,
                    confidence_penalty_max=config.SR_CONFIDENCE_PENALTY_MAX,
                    confidence_bonus_max=config.SR_CONFIDENCE_BONUS_MAX,
                    penalty_proximity_atr=config.SR_PENALTY_PROXIMITY_ATR,
                    penalty_min_touches=config.SR_PENALTY_MIN_TOUCHES,
                )
                # Check if near strong zone (for scenario)
                near_zone, zone_info = is_near_strong_zone(
                    sr_zones, current_price, atr_for_sr,
                    min_touches=config.SR_SCENARIO_MIN_TOUCHES,
                )
                sr_brain_data = {
                    "confidence_adjustment": 0.0,  # Will be set after direction is known
                    "confirmations": [],
                    "alerts": [],
                    "description": sr_context.description,
                    "near_strong_zone": near_zone,
                    "near_zone_info": {
                        "midpoint": zone_info.midpoint,
                        "touches": zone_info.touches,
                        "zone_type": zone_info.zone_type,
                    } if zone_info else None,
                    "all_zones": sr_zones,
                }

        # ============================================================
        # Brain Analysis
        # ============================================================
        brain_result = analyze_with_brain(
            tech_data, ml_data, momentum_data, NEUTRAL_NEWS,
            current_price,
            calendar_data=NEUTRAL_CALENDAR,
            volatility_status=vol_status,
            m5_data=m5_status,
            sr_data=sr_brain_data,
        )

        decision = brain_result.decision
        confidence = brain_result.confidence
        final_score = brain_result.final_score

        # Log for Feb 16 validation
        if h1_time.date() == datetime(2026, 2, 16).date() and 3 <= h1_time.hour <= 10:
            decisions_log.append({
                'time': h1_time.strftime('%H:%M'),
                'decision': decision,
                'score': final_score,
                'confidence': confidence,
                'scenario': brain_result.scenario,
                'tech': tech_data.get('score', 50),
                'ml': ml_data.get('score', 50),
                'momentum': momentum_data.get('score', 50),
                'vol_status': vol_status.get('status', 'NORMAL'),
            })

        # ============================================================
        # Check if actionable
        # ============================================================
        if not is_actionable_signal(decision):
            continue

        if confidence < config.BRAIN_MIN_CONFIDENCE:
            continue

        direction = get_trade_direction(decision)
        if direction is None:
            continue

        # ============================================================
        # S/R direction-aware confidence adjustment
        # ============================================================
        sr_conf_adj_applied = 0.0
        if sr_enabled and sr_zones:
            atr_for_sr = get_atr_value(h1_slice)
            sr_dir_ctx = get_sr_context(
                sr_zones, current_price, atr_for_sr, direction=direction,
                confidence_penalty_max=config.SR_CONFIDENCE_PENALTY_MAX,
                confidence_bonus_max=config.SR_CONFIDENCE_BONUS_MAX,
                penalty_proximity_atr=config.SR_PENALTY_PROXIMITY_ATR,
                penalty_min_touches=config.SR_PENALTY_MIN_TOUCHES,
            )
            sr_conf_adj_applied = sr_dir_ctx.confidence_adjustment
            if sr_conf_adj_applied != 0:
                confidence = max(0, min(100, confidence + sr_conf_adj_applied))

        # ============================================================
        # MTF Trend Confirmation (backtest mode - use historical data)
        # ============================================================
        if getattr(config, 'MTF_TREND_ENABLED', True) and len(df_d1) > 0:
            d1_trend, h4_trend = compute_mtf_trend(df_d1, df_h4, h1_time, 
                                                    ema_period=getattr(config, 'MTF_EMA_PERIOD', 50))
            mtf_adj, mtf_confs, mtf_alerts = _check_mtf_trend_alignment(decision, d1_trend, h4_trend)
            if mtf_adj != 0:
                confidence = max(0, min(100, confidence + mtf_adj))

        # ============================================================
        # Filters
        # ============================================================

        # Anti-gap buffer: block trades near daily close (20:00-21:00) and open (22:00-23:00)
        h1_hour = h1_time.hour
        close_hour = config.MARKET_DAILY_CLOSE_HOUR  # 21
        open_hour = config.MARKET_DAILY_OPEN_HOUR    # 22
        close_buffer = config.MARKET_CLOSE_BUFFER_MINUTES  # 60
        open_buffer = getattr(config, 'MARKET_OPEN_BUFFER_MINUTES', 60)
        # Block if within close_buffer minutes before close
        minutes_to_close = (close_hour * 60) - (h1_hour * 60 + 0)
        if 0 <= minutes_to_close <= close_buffer:
            continue
        # Block if within open_buffer minutes after open
        minutes_after_open = (h1_hour * 60) - (open_hour * 60)
        if 0 <= minutes_after_open < open_buffer:
            continue

        # M5 reversal
        m5_rev = compute_m5_reversal(df_m5, h1_time, direction)
        if m5_rev['reversal_detected']:
            if m5_rev['reversal_strength'] == "strong":
                continue
            elif m5_rev['reversal_strength'] == "moderate":
                confidence -= config.M5_REVERSAL_CONFIDENCE_PENALTY
                if confidence < config.BRAIN_MIN_CONFIDENCE:
                    continue

        # Overtrading cooldown
        lt = last_trade_time.get(direction)
        if lt is not None:
            ct = last_close_type.get(direction)
            if ct == "trailing":
                min_min = config.MIN_MINUTES_AFTER_TRAILING
            elif ct == "sl":
                min_min = config.MIN_MINUTES_AFTER_SL
            else:
                min_min = config.MIN_MINUTES_BETWEEN_TRADES
            elapsed = (h1_time - lt).total_seconds() / 60
            if elapsed < min_min:
                continue

        # Smart pyramid: block if same-direction trade open and not in profit
        same_dir_open = [t for t in open_trades if t.direction == direction]
        is_pyramid_attempt = len(same_dir_open) > 0

        if is_pyramid_attempt:
            pyramid_stats['attempts'] += 1

            if disable_pyramid:
                # Pyramid OFF mode: block all 2nd positions in same direction
                pyramid_stats['blocked_profit'] += 1
                pyramid_stats['blocked_details'].append({
                    'time': h1_time, 'direction': direction,
                    'existing_profit_pct': None, 'reason': 'pyramid_disabled',
                })
                continue

            blocked = False
            worst_profit_pct = None
            for t in same_dir_open:
                if t.direction == "BUY":
                    profit_pct = ((current_price - t.entry_price) / t.entry_price) * 100
                else:
                    profit_pct = ((t.entry_price - current_price) / t.entry_price) * 100
                if worst_profit_pct is None or profit_pct < worst_profit_pct:
                    worst_profit_pct = profit_pct
                if profit_pct < config.PYRAMID_MIN_PROFIT_PERCENT:
                    blocked = True
                    break
            if blocked:
                pyramid_stats['blocked_profit'] += 1
                pyramid_stats['blocked_details'].append({
                    'time': h1_time, 'direction': direction,
                    'existing_profit_pct': round(worst_profit_pct, 4) if worst_profit_pct is not None else None,
                    'reason': f'profit {worst_profit_pct:.3f}% < {config.PYRAMID_MIN_PROFIT_PERCENT}%',
                })
                continue

        # Max positions
        if len(open_trades) >= config.MAX_POSITIONS:
            if is_pyramid_attempt:
                pyramid_stats['blocked_max_pos'] += 1
                pyramid_stats['blocked_details'].append({
                    'time': h1_time, 'direction': direction,
                    'existing_profit_pct': None, 'reason': f'max_positions ({config.MAX_POSITIONS})',
                })
            continue

        # ============================================================
        # Open trade
        # ============================================================
        atr = get_atr_value(h1_slice)
        levels = calculate_sl_tp(current_price, direction, atr)

        # S/R SL/TP adjustment
        sr_sl_tp_desc = ""
        if sr_enabled and sr_zones:
            adj_sl, adj_tp, sr_sl_tp_desc = adjust_sl_tp_for_sr(
                current_price, levels.stop_loss, levels.take_profit_1,
                direction, atr, sr_zones,
                sl_adjust_enabled=config.SR_SL_ADJUST_ENABLED,
                tp_adjust_enabled=(sr_tp_adjust and config.SR_TP_ADJUST_ENABLED),
                min_zone_touches=config.SR_MIN_TOUCHES + 1,  # 3 touches for SL/TP adj
            )
            if adj_sl != levels.stop_loss or adj_tp != levels.take_profit_1:
                from risk_manager import StopLevels
                sl_pips = abs(current_price - adj_sl) / PIP_SIZE
                tp1_pips = abs(adj_tp - current_price) / PIP_SIZE
                rr1 = tp1_pips / sl_pips if sl_pips > 0 else 0
                levels = StopLevels(
                    entry_price=current_price, stop_loss=adj_sl,
                    take_profit_1=adj_tp, take_profit_2=adj_tp,
                    sl_pips=sl_pips, tp1_pips=tp1_pips, tp2_pips=tp1_pips,
                    risk_reward_1=rr1, risk_reward_2=rr1,
                )

        # Track pyramid allowed
        if is_pyramid_attempt:
            pyramid_stats['allowed'] += 1

        # Detect candlestick patterns with S/R proximity scaling
        from technical_analyzer import detect_candlestick_patterns
        pattern_result = detect_candlestick_patterns(
            h1_slice, sr_zones=sr_zones, current_price=current_price, atr=atr
        )
        pattern_name = ""
        pattern_score = 0.0
        pattern_sr_mult = 1.0
        primary_pattern = pattern_result.get("primary_pattern")
        if primary_pattern:
            pattern_name = primary_pattern.get("name", "")
            pattern_score = primary_pattern.get("final_score", 0.0)
            pattern_sr_mult = primary_pattern.get("sr_multiplier", 1.0)

        ticket_counter += 1
        trade = SimTrade(
            ticket=ticket_counter,
            direction=direction,
            entry_price=current_price,
            entry_time=h1_time,
            sl=levels.stop_loss,
            tp=levels.take_profit_1,
            atr=atr,
            brain_score=final_score,
            confidence=confidence,
            scenario=brain_result.scenario,
            scenario_desc=brain_result.scenario_description,
            explanation_snippet=brain_result.explanation[:300],
            is_pyramid=is_pyramid_attempt,
            tech_score=tech_data.get('score', 50),
            ml_score=ml_data.get('score', 50),
            momentum_score=momentum_data.get('score', 50),
            confirmations=brain_result.confirmations[:5],
            alerts=brain_result.alerts[:5],
            candlestick_pattern=pattern_name,
            candlestick_score=pattern_score,
            candlestick_sr_mult=pattern_sr_mult,
        )

        # Simulate trade immediately using M5 data (for pyramid/cooldown tracking in H1 loop)
        trade = simulate_trade(trade, df_m5)
        trades.append(trade)
        open_trades.append(trade)

        # Record for overtrading
        last_trade_time[direction] = h1_time

    # Store decisions log for Feb 16 validation
    if decisions_log:
        run_backtest._feb16_decisions = decisions_log

    # ============================================================
    # Re-simulate with Early Exit (concurrent awareness)
    # ============================================================
    if early_exit and trades:
        # Create fresh copies of trades (reset close fields, keep entry info)
        trades_for_resim = []
        for t in trades:
            fresh = SimTrade(
                ticket=t.ticket, direction=t.direction,
                entry_price=t.entry_price, entry_time=t.entry_time,
                sl=t.sl, tp=t.tp, atr=t.atr,
                brain_score=t.brain_score, confidence=t.confidence,
                scenario=t.scenario, scenario_desc=t.scenario_desc,
                explanation_snippet=t.explanation_snippet,
                is_pyramid=t.is_pyramid,
                tech_score=t.tech_score, ml_score=t.ml_score,
                momentum_score=t.momentum_score,
                confirmations=list(t.confirmations), alerts=list(t.alerts),
                candlestick_pattern=t.candlestick_pattern,
                candlestick_score=t.candlestick_score,
                candlestick_sr_mult=t.candlestick_sr_mult,
            )
            trades_for_resim.append(fresh)

        print(f"   Re-simulating {len(trades_for_resim)} trades with Early Exit enabled...")
        trades = simulate_trades_concurrent(trades_for_resim, df_m5, early_exit_enabled=True)

        early_exits = [t for t in trades if t.early_exit_reason]
        if early_exits:
            print(f"   Early Exit triggered on {len(early_exits)} trades:")
            for t in early_exits:
                print(f"     #{t.ticket} {t.direction} @ {t.entry_time.strftime('%m-%d %H:%M')} → {t.early_exit_reason} P&L={t.profit_pips:+.1f} pips")

    return trades, pyramid_stats


# ============================================================================
# REPORT
# ============================================================================

def generate_report(trades: List[SimTrade], trades_no_visual: Optional[List[SimTrade]] = None,
                    pyramid_stats: Optional[Dict] = None,
                    trades_no_pyramid: Optional[List[SimTrade]] = None,
                    pyramid_stats_off: Optional[Dict] = None,
                    trades_no_early_exit: Optional[List[SimTrade]] = None,
                    sr_comparison: Optional[Dict] = None,
                    pattern_comparison: Optional[Dict] = None) -> str:
    """Generate comprehensive backtest report."""
    lines = []
    lines.append("=" * 70)
    lines.append("  BACKTEST REPORT — XAUUSD Trading Bot (Central Brain)")
    lines.append(f"  Period: {BT_START.strftime('%Y-%m-%d')} → {BT_END.strftime('%Y-%m-%d %H:%M')}")
    lines.append(f"  News = 50 (neutral) | Calendar = 50 (neutral)")
    lines.append("=" * 70)

    if not trades:
        lines.append("\n❌ No trades generated.")
        return "\n".join(lines)

    # ============================================================
    # SUMMARY
    # ============================================================
    total = len(trades)
    wins = [t for t in trades if t.profit_pips > 0]
    losses = [t for t in trades if t.profit_pips <= 0]
    win_rate = len(wins) / total * 100 if total > 0 else 0
    total_pnl = sum(t.profit_usd for t in trades)
    total_pips = sum(t.profit_pips for t in trades)
    avg_win = np.mean([t.profit_pips for t in wins]) if wins else 0
    avg_loss = np.mean([t.profit_pips for t in losses]) if losses else 0
    gross_profit = sum(t.profit_usd for t in wins)
    gross_loss = abs(sum(t.profit_usd for t in losses))
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else float('inf')

    # Max drawdown (sequential)
    running_pnl = 0
    peak = 0
    max_dd = 0
    for t in trades:
        running_pnl += t.profit_usd
        peak = max(peak, running_pnl)
        dd = peak - running_pnl
        max_dd = max(max_dd, dd)

    buys = [t for t in trades if t.direction == "BUY"]
    sells = [t for t in trades if t.direction == "SELL"]

    lines.append(f"\n{'─' * 50}")
    lines.append(f"  📊 SUMMARY")
    lines.append(f"{'─' * 50}")
    lines.append(f"  Total trades:     {total}")
    lines.append(f"  Wins:             {len(wins)} ({win_rate:.1f}%)")
    lines.append(f"  Losses:           {len(losses)}")
    lines.append(f"  Total P&L:        ${total_pnl:+.2f} ({total_pips:+.1f} pips)")
    lines.append(f"  Avg win:          {avg_win:+.1f} pips")
    lines.append(f"  Avg loss:         {avg_loss:+.1f} pips")
    lines.append(f"  Profit factor:    {profit_factor:.2f}")
    lines.append(f"  Max drawdown:     ${max_dd:.2f}")
    lines.append(f"  BUY trades:       {len(buys)} (W:{sum(1 for t in buys if t.profit_pips > 0)} L:{sum(1 for t in buys if t.profit_pips <= 0)})")
    lines.append(f"  SELL trades:      {len(sells)} (W:{sum(1 for t in sells if t.profit_pips > 0)} L:{sum(1 for t in sells if t.profit_pips <= 0)})")

    # By scenario
    scenarios = {}
    for t in trades:
        s = t.scenario
        if s not in scenarios:
            scenarios[s] = {'count': 0, 'wins': 0, 'pnl': 0.0}
        scenarios[s]['count'] += 1
        if t.profit_pips > 0:
            scenarios[s]['wins'] += 1
        scenarios[s]['pnl'] += t.profit_usd

    lines.append(f"\n  By scenario:")
    for s, d in sorted(scenarios.items(), key=lambda x: -x[1]['count']):
        wr = d['wins'] / d['count'] * 100 if d['count'] > 0 else 0
        lines.append(f"    {s}: {d['count']} trades, WR {wr:.0f}%, P&L ${d['pnl']:+.2f}")

    # ============================================================
    # TRADE LIST
    # ============================================================
    lines.append(f"\n{'─' * 50}")
    lines.append(f"  📋 ALL TRADES")
    lines.append(f"{'─' * 50}")
    lines.append(f"  {'#':>3} {'Dir':>4} {'Entry Time':>16} {'Entry':>9} {'SL':>9} {'TP':>9} {'Exit':>9} {'Reason':>8} {'P&L':>8} {'Pips':>7} {'Dur':>6} {'Score':>5} {'Conf':>5} {'Scenario':>20}")

    for i, t in enumerate(trades, 1):
        entry_str = t.entry_time.strftime('%m-%d %H:%M') if t.entry_time else '?'
        dur_str = f"{t.duration_minutes:.0f}m" if t.duration_minutes else "?"
        lines.append(
            f"  {i:>3} {t.direction:>4} {entry_str:>16} {t.entry_price:>9.2f} "
            f"{t.sl:>9.2f} {t.tp:>9.2f} {t.close_price:>9.2f} {t.close_reason:>8} "
            f"${t.profit_usd:>+7.2f} {t.profit_pips:>+6.1f} {dur_str:>6} "
            f"{t.brain_score:>5.1f} {t.confidence:>5.1f} {t.scenario:>20}"
        )

    # ============================================================
    # LOSING TRADES ANALYSIS
    # ============================================================
    if losses:
        lines.append(f"\n{'─' * 50}")
        lines.append(f"  🔴 LOSING TRADES ANALYSIS")
        lines.append(f"{'─' * 50}")

        for t in losses:
            entry_str = t.entry_time.strftime('%Y-%m-%d %H:%M') if t.entry_time else '?'
            lines.append(f"\n  Trade #{t.ticket} — {t.direction} @ {entry_str}")
            lines.append(f"    Entry: {t.entry_price:.2f} | SL: {t.sl:.2f} | TP: {t.tp:.2f}")
            lines.append(f"    Exit: {t.close_price:.2f} ({t.close_reason}) | P&L: {t.profit_pips:+.1f} pips (${t.profit_usd:+.2f})")
            lines.append(f"    Max favorable: {t.max_favorable_pips:.1f} pips | Max adverse: {t.max_adverse_pips:.1f} pips")
            lines.append(f"    Duration: {t.duration_minutes:.0f} min")
            lines.append(f"    Brain: score={t.brain_score:.1f}, conf={t.confidence:.1f}, scenario={t.scenario}")
            lines.append(f"    Pillars: Tech={t.tech_score:.1f}, ML={t.ml_score:.1f}, Momentum={t.momentum_score:.1f}")
            if t.confirmations:
                lines.append(f"    Confirmations: {'; '.join(t.confirmations[:3])}")
            if t.alerts:
                lines.append(f"    Alerts: {'; '.join(t.alerts[:3])}")
            # Why did it lose?
            if t.max_favorable_pips > 50:
                lines.append(f"    ⚠️ Had {t.max_favorable_pips:.0f} pips favorable before reversing — trailing could have saved it")
            if t.close_reason == "sl_gap":
                lines.append(f"    ⚠️ Gap at session open — SL slipped")

    # ============================================================
    # VISUAL FEATURES COMPARISON
    # ============================================================
    if trades_no_visual is not None:
        lines.append(f"\n{'─' * 50}")
        lines.append(f"  🔬 VISUAL FEATURES COMPARISON")
        lines.append(f"{'─' * 50}")

        nv_total = len(trades_no_visual)
        nv_wins = sum(1 for t in trades_no_visual if t.profit_pips > 0)
        nv_pnl = sum(t.profit_usd for t in trades_no_visual)
        nv_wr = nv_wins / nv_total * 100 if nv_total > 0 else 0

        lines.append(f"  WITH visual features:    {total} trades, WR {win_rate:.1f}%, P&L ${total_pnl:+.2f}")
        lines.append(f"  WITHOUT visual features: {nv_total} trades, WR {nv_wr:.1f}%, P&L ${nv_pnl:+.2f}")

        # Trades that differ
        with_tickets = {(t.entry_time, t.direction) for t in trades}
        without_tickets = {(t.entry_time, t.direction) for t in trades_no_visual}
        only_with = with_tickets - without_tickets
        only_without = without_tickets - with_tickets

        if only_with:
            lines.append(f"  Trades ONLY with visual features ({len(only_with)}):")
            for t in trades:
                if (t.entry_time, t.direction) in only_with:
                    lines.append(f"    {t.entry_time.strftime('%m-%d %H:%M')} {t.direction} P&L={t.profit_pips:+.1f} pips")
        if only_without:
            lines.append(f"  Trades ONLY without visual features ({len(only_without)}):")
            for t in trades_no_visual:
                if (t.entry_time, t.direction) in only_without:
                    lines.append(f"    {t.entry_time.strftime('%m-%d %H:%M')} {t.direction} P&L={t.profit_pips:+.1f} pips")

    # ============================================================
    # SMART PYRAMID ANALYSIS
    # ============================================================
    if pyramid_stats is not None:
        lines.append(f"\n{'─' * 50}")
        lines.append(f"  🔺 SMART PYRAMID ANALYSIS")
        lines.append(f"{'─' * 50}")
        lines.append(f"  Threshold: {config.PYRAMID_MIN_PROFIT_PERCENT}% | Max positions: {config.MAX_POSITIONS}")
        lines.append(f"  Attempts (2nd pos same dir):  {pyramid_stats['attempts']}")
        lines.append(f"  Blocked (profit < threshold): {pyramid_stats['blocked_profit']}")
        lines.append(f"  Blocked (max positions):      {pyramid_stats['blocked_max_pos']}")
        lines.append(f"  Allowed (pyramid opened):     {pyramid_stats['allowed']}")

        # Blocked details
        blocked = [d for d in pyramid_stats['blocked_details'] if d['reason'] != 'pyramid_disabled']
        if blocked:
            lines.append(f"\n  Blocked attempts detail:")
            lines.append(f"  {'Time':>16} {'Dir':>4} {'Existing P%':>12} {'Reason':>30}")
            for d in blocked:
                t_str = d['time'].strftime('%m-%d %H:%M') if d['time'] else '?'
                p_str = f"{d['existing_profit_pct']:.3f}%" if d['existing_profit_pct'] is not None else 'N/A'
                lines.append(f"  {t_str:>16} {d['direction']:>4} {p_str:>12} {d['reason']:>30}")

        # P&L: normal vs pyramid trades
        normal_trades = [t for t in trades if not t.is_pyramid]
        pyramid_trades = [t for t in trades if t.is_pyramid]

        if pyramid_trades:
            lines.append(f"\n  Pyramided trades:")
            lines.append(f"  {'#':>3} {'Dir':>4} {'Entry Time':>16} {'Entry':>9} {'Exit':>9} {'Reason':>8} {'P&L':>8} {'Pips':>7}")
            for i, t in enumerate(pyramid_trades, 1):
                entry_str = t.entry_time.strftime('%m-%d %H:%M') if t.entry_time else '?'
                lines.append(
                    f"  {i:>3} {t.direction:>4} {entry_str:>16} {t.entry_price:>9.2f} "
                    f"{t.close_price:>9.2f} {t.close_reason:>8} ${t.profit_usd:>+7.2f} {t.profit_pips:>+6.1f}"
                )

        n_count = len(normal_trades)
        p_count = len(pyramid_trades)
        n_wins = sum(1 for t in normal_trades if t.profit_pips > 0)
        p_wins = sum(1 for t in pyramid_trades if t.profit_pips > 0)
        n_pnl = sum(t.profit_usd for t in normal_trades)
        p_pnl = sum(t.profit_usd for t in pyramid_trades)
        n_pips = sum(t.profit_pips for t in normal_trades)
        p_pips = sum(t.profit_pips for t in pyramid_trades)
        n_wr = n_wins / n_count * 100 if n_count > 0 else 0
        p_wr = p_wins / p_count * 100 if p_count > 0 else 0

        lines.append(f"\n  P&L breakdown:")
        lines.append(f"  {'':>20} {'Trades':>7} {'Wins':>5} {'WR%':>7} {'P&L $':>10} {'Pips':>8}")
        lines.append(f"  {'Normal':>20} {n_count:>7} {n_wins:>5} {n_wr:>6.1f}% ${n_pnl:>+9.2f} {n_pips:>+7.1f}")
        lines.append(f"  {'Pyramid':>20} {p_count:>7} {p_wins:>5} {p_wr:>6.1f}% ${p_pnl:>+9.2f} {p_pips:>+7.1f}")
        lines.append(f"  {'TOTAL':>20} {total:>7} {len(wins):>5} {win_rate:>6.1f}% ${total_pnl:>+9.2f} {total_pips:>+7.1f}")

    # ============================================================
    # PYRAMID ON vs OFF COMPARISON
    # ============================================================
    if trades_no_pyramid is not None:
        lines.append(f"\n{'─' * 50}")
        lines.append(f"  🔺 PYRAMID ON vs OFF COMPARISON")
        lines.append(f"{'─' * 50}")

        def _calc_stats(tlist):
            if not tlist:
                return {'trades': 0, 'wins': 0, 'wr': 0, 'pnl': 0, 'pips': 0, 'pf': 0, 'max_dd': 0}
            w = [t for t in tlist if t.profit_pips > 0]
            l = [t for t in tlist if t.profit_pips <= 0]
            gp = sum(t.profit_usd for t in w)
            gl = abs(sum(t.profit_usd for t in l))
            rp, pk, mdd = 0, 0, 0
            for t in tlist:
                rp += t.profit_usd
                pk = max(pk, rp)
                mdd = max(mdd, pk - rp)
            return {
                'trades': len(tlist), 'wins': len(w),
                'wr': len(w) / len(tlist) * 100,
                'pnl': sum(t.profit_usd for t in tlist),
                'pips': sum(t.profit_pips for t in tlist),
                'pf': gp / gl if gl > 0 else float('inf'),
                'max_dd': mdd,
            }

        on = _calc_stats(trades)
        off = _calc_stats(trades_no_pyramid)

        lines.append(f"  {'':>20} {'Pyramid ON':>14} {'Pyramid OFF':>14} {'Delta':>10}")
        lines.append(f"  {'Trades':>20} {on['trades']:>14} {off['trades']:>14} {on['trades']-off['trades']:>+10}")
        lines.append(f"  {'Win Rate':>20} {on['wr']:>13.1f}% {off['wr']:>13.1f}% {on['wr']-off['wr']:>+9.1f}%")
        lines.append(f"  {'P&L $':>20} ${on['pnl']:>+12.2f} ${off['pnl']:>+12.2f} ${on['pnl']-off['pnl']:>+9.2f}")
        lines.append(f"  {'Pips':>20} {on['pips']:>+13.1f} {off['pips']:>+13.1f} {on['pips']-off['pips']:>+9.1f}")
        lines.append(f"  {'Profit Factor':>20} {on['pf']:>14.2f} {off['pf']:>14.2f}")
        lines.append(f"  {'Max Drawdown':>20} ${on['max_dd']:>12.2f} ${off['max_dd']:>12.2f}")

        # Verdict
        delta_pnl = on['pnl'] - off['pnl']
        if delta_pnl > 0:
            lines.append(f"\n  ✅ Pyramid ON is BETTER by ${delta_pnl:+.2f}")
        elif delta_pnl < 0:
            lines.append(f"\n  ❌ Pyramid ON is WORSE by ${delta_pnl:+.2f}")
        else:
            lines.append(f"\n  ➖ Pyramid ON = OFF (no difference)")

    # ============================================================
    # EARLY EXIT ON vs OFF COMPARISON
    # ============================================================
    if trades_no_early_exit is not None:
        lines.append(f"\n{'─' * 50}")
        lines.append(f"  🚨 EARLY EXIT ON vs OFF COMPARISON")
        lines.append(f"{'─' * 50}")
        lines.append(f"  Thresholds: Pyramid DD={config.PYRAMID_EXIT_DRAWDOWN_PIPS} pips, "
                     f"Combined={config.PYRAMID_EXIT_COMBINED_DRAWDOWN_PCT}%, "
                     f"Speed={config.PYRAMID_EXIT_SPEED_PIPS} pips/{config.PYRAMID_EXIT_SPEED_MINUTES}min, "
                     f"Extreme min loss={config.EXTREME_EXIT_MIN_LOSS_PIPS} pips")

        def _calc_stats_ee(tlist):
            if not tlist:
                return {'trades': 0, 'wins': 0, 'wr': 0, 'pnl': 0, 'pips': 0, 'pf': 0, 'max_dd': 0}
            w = [t for t in tlist if t.profit_pips > 0]
            l = [t for t in tlist if t.profit_pips <= 0]
            gp = sum(t.profit_usd for t in w)
            gl = abs(sum(t.profit_usd for t in l))
            rp, pk, mdd = 0, 0, 0
            for t in tlist:
                rp += t.profit_usd
                pk = max(pk, rp)
                mdd = max(mdd, pk - rp)
            return {
                'trades': len(tlist), 'wins': len(w),
                'wr': len(w) / len(tlist) * 100,
                'pnl': sum(t.profit_usd for t in tlist),
                'pips': sum(t.profit_pips for t in tlist),
                'pf': gp / gl if gl > 0 else float('inf'),
                'max_dd': mdd,
            }

        ee_on = _calc_stats_ee(trades)
        ee_off = _calc_stats_ee(trades_no_early_exit)

        lines.append(f"\n  {'':>20} {'EE ON':>14} {'EE OFF':>14} {'Delta':>10}")
        lines.append(f"  {'Trades':>20} {ee_on['trades']:>14} {ee_off['trades']:>14} {ee_on['trades']-ee_off['trades']:>+10}")
        lines.append(f"  {'Win Rate':>20} {ee_on['wr']:>13.1f}% {ee_off['wr']:>13.1f}% {ee_on['wr']-ee_off['wr']:>+9.1f}%")
        lines.append(f"  {'P&L $':>20} ${ee_on['pnl']:>+12.2f} ${ee_off['pnl']:>+12.2f} ${ee_on['pnl']-ee_off['pnl']:>+9.2f}")
        lines.append(f"  {'Pips':>20} {ee_on['pips']:>+13.1f} {ee_off['pips']:>+13.1f} {ee_on['pips']-ee_off['pips']:>+9.1f}")
        lines.append(f"  {'Profit Factor':>20} {ee_on['pf']:>14.2f} {ee_off['pf']:>14.2f}")
        lines.append(f"  {'Max Drawdown':>20} ${ee_on['max_dd']:>12.2f} ${ee_off['max_dd']:>12.2f}")

        # Verdict
        delta_pnl = ee_on['pnl'] - ee_off['pnl']
        if delta_pnl > 0:
            lines.append(f"\n  ✅ Early Exit ON is BETTER by ${delta_pnl:+.2f}")
        elif delta_pnl < 0:
            lines.append(f"\n  ❌ Early Exit ON is WORSE by ${delta_pnl:+.2f}")
        else:
            lines.append(f"\n  ➖ Early Exit ON = OFF (no difference)")

        # Early Exit event details
        early_exit_trades = [t for t in trades if t.early_exit_reason]
        if early_exit_trades:
            # Group by reason
            by_reason = {}
            for t in early_exit_trades:
                r = t.early_exit_reason
                if r not in by_reason:
                    by_reason[r] = []
                by_reason[r].append(t)

            lines.append(f"\n  Early Exit Events: {len(early_exit_trades)} total")
            for reason, reason_trades in sorted(by_reason.items()):
                avg_pnl = np.mean([t.profit_pips for t in reason_trades])
                lines.append(f"    {reason}: {len(reason_trades)} triggers, avg P&L={avg_pnl:+.1f} pips")

            # Detail each early exit event
            lines.append(f"\n  {'#':>3} {'Dir':>4} {'Entry Time':>16} {'Entry':>9} {'Exit':>9} {'P&L':>8} {'Pips':>7} {'Reason':>30} {'Pyramid':>7}")
            for i, t in enumerate(early_exit_trades, 1):
                entry_str = t.entry_time.strftime('%m-%d %H:%M') if t.entry_time else '?'
                lines.append(
                    f"  {i:>3} {t.direction:>4} {entry_str:>16} {t.entry_price:>9.2f} "
                    f"{t.close_price:>9.2f} ${t.profit_usd:>+7.2f} {t.profit_pips:>+6.1f} "
                    f"{t.early_exit_reason:>30} {'Yes' if t.is_pyramid else 'No':>7}"
                )

            # Would-have-recovered analysis
            # Compare each early-exited trade with its counterpart in the no-early-exit run
            lines.append(f"\n  Would-have-recovered analysis:")
            recovered_count = 0
            saved_count = 0
            total_saved_pips = 0.0
            total_lost_opportunity_pips = 0.0

            # Build lookup from no-early-exit trades by ticket
            off_by_ticket = {t.ticket: t for t in trades_no_early_exit}

            for t in early_exit_trades:
                off_trade = off_by_ticket.get(t.ticket)
                if off_trade is None:
                    continue

                ee_pips = t.profit_pips
                no_ee_pips = off_trade.profit_pips

                if no_ee_pips > ee_pips:
                    # Would have been better without early exit
                    recovered_count += 1
                    total_lost_opportunity_pips += (no_ee_pips - ee_pips)
                    lines.append(f"    #{t.ticket} {t.direction} {t.entry_time.strftime('%m-%d %H:%M')}: "
                                f"EE={ee_pips:+.1f} pips, NoEE={no_ee_pips:+.1f} pips → "
                                f"WOULD HAVE RECOVERED ({no_ee_pips - ee_pips:+.1f} pips lost opportunity)")
                else:
                    # Early exit saved money
                    saved_count += 1
                    total_saved_pips += (ee_pips - no_ee_pips)
                    lines.append(f"    #{t.ticket} {t.direction} {t.entry_time.strftime('%m-%d %H:%M')}: "
                                f"EE={ee_pips:+.1f} pips, NoEE={no_ee_pips:+.1f} pips → "
                                f"SAVED ({ee_pips - no_ee_pips:+.1f} pips)")

            total_ee = len(early_exit_trades)
            recovered_pct = recovered_count / total_ee * 100 if total_ee > 0 else 0
            saved_pct = saved_count / total_ee * 100 if total_ee > 0 else 0

            lines.append(f"\n  Summary:")
            lines.append(f"    Trades where EE saved money:      {saved_count} ({saved_pct:.0f}%) — total saved: {total_saved_pips:+.1f} pips")
            lines.append(f"    Trades that would have recovered: {recovered_count} ({recovered_pct:.0f}%) — total lost: {total_lost_opportunity_pips:+.1f} pips")
            lines.append(f"    Net impact: {total_saved_pips - total_lost_opportunity_pips:+.1f} pips")

            if recovered_pct > 30:
                lines.append(f"\n  ⚠️ WARNING: {recovered_pct:.0f}% of early exits would have recovered — thresholds may be too aggressive!")
            elif recovered_pct <= 20:
                lines.append(f"\n  ✅ Only {recovered_pct:.0f}% would have recovered — thresholds look appropriate.")
        else:
            lines.append(f"\n  No Early Exit events triggered in this period.")

    # ============================================================
    # S/R COMPARISON (3-way)
    # ============================================================
    if sr_comparison is not None:
        lines.append(f"\n{'─' * 50}")
        lines.append(f"  🎯 SUPPORT & RESISTANCE — 3-WAY COMPARISON")
        lines.append(f"{'─' * 50}")

        def _calc_stats_sr(tlist):
            if not tlist:
                return {'trades': 0, 'wins': 0, 'wr': 0, 'pnl': 0, 'pips': 0, 'pf': 0, 'max_dd': 0,
                        'dd_peak_idx': 0, 'dd_trough_idx': 0, 'dd_sequence': []}
            w = [t for t in tlist if t.profit_pips > 0]
            l = [t for t in tlist if t.profit_pips <= 0]
            gp = sum(t.profit_usd for t in w)
            gl = abs(sum(t.profit_usd for t in l))
            rp, pk, mdd = 0, 0, 0
            dd_peak_idx, dd_trough_idx, dd_peak_candidate = 0, 0, 0
            cum_pnl = []
            for i, t in enumerate(tlist):
                rp += t.profit_usd
                cum_pnl.append(rp)
                if rp > pk:
                    pk = rp
                    dd_peak_candidate = i
                dd = pk - rp
                if dd > mdd:
                    mdd = dd
                    dd_trough_idx = i
                    dd_peak_idx = dd_peak_candidate
            # Build DD sequence with cumulative P&L
            dd_seq = []
            if mdd > 0:
                start = max(dd_peak_idx - 1, 0)
                end = min(dd_trough_idx + 2, len(tlist))
                for i in range(start, end):
                    t = tlist[i]
                    dd_seq.append({
                        'idx': i + 1, 'time': t.entry_time, 'dir': t.direction,
                        'pnl_usd': t.profit_usd, 'pnl_pips': t.profit_pips,
                        'cum_pnl': cum_pnl[i], 'scenario': t.scenario,
                        'is_peak': i == dd_peak_idx, 'is_trough': i == dd_trough_idx,
                    })
            return {
                'trades': len(tlist), 'wins': len(w),
                'wr': len(w) / len(tlist) * 100,
                'pnl': sum(t.profit_usd for t in tlist),
                'pips': sum(t.profit_pips for t in tlist),
                'pf': gp / gl if gl > 0 else float('inf'),
                'max_dd': mdd,
                'dd_peak_idx': dd_peak_idx, 'dd_trough_idx': dd_trough_idx,
                'dd_sequence': dd_seq,
            }

        baseline = _calc_stats_sr(sr_comparison.get('baseline', []))
        sr_tp_on = _calc_stats_sr(sr_comparison.get('sr_tp_on', []))
        sr_tp_off = _calc_stats_sr(sr_comparison.get('sr_tp_off', []))

        lines.append(f"\n  {'':>20} {'Baseline':>14} {'SR+TP ON':>14} {'SR+TP OFF':>14}")
        lines.append(f"  {'Trades':>20} {baseline['trades']:>14} {sr_tp_on['trades']:>14} {sr_tp_off['trades']:>14}")
        lines.append(f"  {'Win Rate':>20} {baseline['wr']:>13.1f}% {sr_tp_on['wr']:>13.1f}% {sr_tp_off['wr']:>13.1f}%")
        lines.append(f"  {'P&L $':>20} ${baseline['pnl']:>+12.2f} ${sr_tp_on['pnl']:>+12.2f} ${sr_tp_off['pnl']:>+12.2f}")
        lines.append(f"  {'Pips':>20} {baseline['pips']:>+13.1f} {sr_tp_on['pips']:>+13.1f} {sr_tp_off['pips']:>+13.1f}")
        lines.append(f"  {'Profit Factor':>20} {baseline['pf']:>14.2f} {sr_tp_on['pf']:>14.2f} {sr_tp_off['pf']:>14.2f}")
        lines.append(f"  {'Max Drawdown':>20} ${baseline['max_dd']:>12.2f} ${sr_tp_on['max_dd']:>12.2f} ${sr_tp_off['max_dd']:>12.2f}")

        # Verdict
        best_label = "Baseline"
        best_pf = baseline['pf']
        if sr_tp_on['pf'] > best_pf:
            best_label = "SR+TP ON"
            best_pf = sr_tp_on['pf']
        if sr_tp_off['pf'] > best_pf:
            best_label = "SR+TP OFF"
            best_pf = sr_tp_off['pf']

        lines.append(f"\n  Best Profit Factor: {best_label} ({best_pf:.2f})")

        # PF degradation check
        for label, stats in [("SR+TP ON", sr_tp_on), ("SR+TP OFF", sr_tp_off)]:
            delta_pf = stats['pf'] - baseline['pf']
            if delta_pf < -0.15:
                lines.append(f"  ❌ {label} degrades PF by {delta_pf:+.2f} — ABANDON")
            elif delta_pf >= 0.10:
                lines.append(f"  ✅ {label} improves PF by {delta_pf:+.2f} — ADOPT")
            else:
                lines.append(f"  ➖ {label} PF delta {delta_pf:+.2f} — neutral")

        # MAX DRAWDOWN DETAIL
        lines.append(f"\n  MAX DRAWDOWN DETAIL:")
        for label, stats in [("Baseline", baseline), ("SR+TP ON", sr_tp_on), ("SR+TP OFF", sr_tp_off)]:
            dd_seq = stats.get('dd_sequence', [])
            if dd_seq:
                lines.append(f"\n  {label} (Max DD: ${stats['max_dd']:.2f}):")
                for d in dd_seq:
                    marker = " <<PEAK" if d['is_peak'] else (" <<TROUGH" if d['is_trough'] else "")
                    lines.append(
                        f"    #{d['idx']:>3} {d['time'].strftime('%m-%d %H:%M')} {d['dir']:>4} "
                        f"${d['pnl_usd']:+8.2f} ({d['pnl_pips']:+7.1f}p) "
                        f"cum=${d['cum_pnl']:+9.2f} {d['scenario']}{marker}"
                    )

        # Penalty monitoring: trades that differ between baseline and SR runs
        baseline_trades = sr_comparison.get('baseline', [])
        sr_on_trades = sr_comparison.get('sr_tp_on', [])

        if baseline_trades and sr_on_trades:
            baseline_keys = {(t.entry_time, t.direction) for t in baseline_trades}
            sr_on_keys = {(t.entry_time, t.direction) for t in sr_on_trades}

            blocked_by_sr = baseline_keys - sr_on_keys
            added_by_sr = sr_on_keys - baseline_keys

            if blocked_by_sr:
                blocked_trades = [t for t in baseline_trades if (t.entry_time, t.direction) in blocked_by_sr]
                blocked_wins = sum(1 for t in blocked_trades if t.profit_pips > 0)
                blocked_losses = sum(1 for t in blocked_trades if t.profit_pips <= 0)
                blocked_pnl = sum(t.profit_usd for t in blocked_trades)

                lines.append(f"\n  S/R PENALTY MONITORING:")
                lines.append(f"  Trades blocked by S/R: {len(blocked_by_sr)}")
                lines.append(f"    Would have been WINS:   {blocked_wins}")
                lines.append(f"    Would have been LOSSES: {blocked_losses}")
                lines.append(f"    Net P&L lost:           ${blocked_pnl:+.2f}")

                # Direction breakdown
                buy_blocked = [t for t in blocked_trades if t.direction == "BUY"]
                sell_blocked = [t for t in blocked_trades if t.direction == "SELL"]
                if buy_blocked:
                    bw = sum(1 for t in buy_blocked if t.profit_pips > 0)
                    lines.append(f"    BUY blocked: {len(buy_blocked)} ({bw}W/{len(buy_blocked)-bw}L) WR={bw/len(buy_blocked)*100:.0f}%")
                if sell_blocked:
                    sw = sum(1 for t in sell_blocked if t.profit_pips > 0)
                    lines.append(f"    SELL blocked: {len(sell_blocked)} ({sw}W/{len(sell_blocked)-sw}L) WR={sw/len(sell_blocked)*100:.0f}%")

                blocked_wr = blocked_wins / len(blocked_trades) * 100 if blocked_trades else 0
                if blocked_wr > 50:
                    lines.append(f"  ⚠️ WARNING: {blocked_wr:.0f}% of blocked trades were winners — penalty NOT discriminating")
                    lines.append(f"  ❌ VERDICT: ABANDON penalty, keep zona_sr_forte scenario only")
                elif blocked_wins > blocked_losses:
                    lines.append(f"  ⚠️ S/R blocked more winners ({blocked_wins}) than losers ({blocked_losses}) — monitor closely")
                else:
                    lines.append(f"  ✅ S/R correctly blocked more losers ({blocked_losses}) than winners ({blocked_wins})")

                lines.append(f"\n  Blocked trade details:")
                for t in blocked_trades:
                    result = "WIN" if t.profit_pips > 0 else "LOSS"
                    lines.append(f"    {t.entry_time.strftime('%m-%d %H:%M')} {t.direction} P&L={t.profit_pips:+.1f} pips ({result}) scenario={t.scenario}")

            if added_by_sr:
                added_trades = [t for t in sr_on_trades if (t.entry_time, t.direction) in added_by_sr]
                added_wins = sum(1 for t in added_trades if t.profit_pips > 0)
                added_pnl = sum(t.profit_usd for t in added_trades)
                lines.append(f"\n  Trades ADDED by S/R (different decisions): {len(added_by_sr)}")
                lines.append(f"    Wins: {added_wins}, P&L: ${added_pnl:+.2f}")

    # ============================================================
    # PATTERNS ON vs OFF COMPARISON
    # ============================================================
    if pattern_comparison is not None:
        lines.append(f"\n{'─' * 50}")
        lines.append(f"  🕯️ PATTERNS ON vs OFF COMPARISON")
        lines.append(f"{'─' * 50}")
        
        def _calc_pattern_stats(tlist):
            if not tlist:
                return {'trades': 0, 'wins': 0, 'wr': 0, 'pnl': 0, 'pips': 0, 'pf': 0}
            w = [t for t in tlist if t.profit_pips > 0]
            l = [t for t in tlist if t.profit_pips <= 0]
            gp = sum(t.profit_usd for t in w)
            gl = abs(sum(t.profit_usd for t in l))
            return {
                'trades': len(tlist), 'wins': len(w),
                'wr': len(w) / len(tlist) * 100,
                'pnl': sum(t.profit_usd for t in tlist),
                'pips': sum(t.profit_pips for t in tlist),
                'pf': gp / gl if gl > 0 else float('inf'),
            }
        
        off_stats = _calc_pattern_stats(pattern_comparison.get('patterns_off', []))
        on_stats = _calc_pattern_stats(pattern_comparison.get('patterns_on', []))
        
        lines.append(f"\n  {'':>20} {'Patterns OFF':>14} {'Patterns ON':>14} {'Delta':>10}")
        lines.append(f"  {'Trades':>20} {off_stats['trades']:>14} {on_stats['trades']:>14} {on_stats['trades']-off_stats['trades']:>+10}")
        lines.append(f"  {'Win Rate':>20} {off_stats['wr']:>13.1f}% {on_stats['wr']:>13.1f}% {on_stats['wr']-off_stats['wr']:>+9.1f}%")
        lines.append(f"  {'P&L $':>20} ${off_stats['pnl']:>+12.2f} ${on_stats['pnl']:>+12.2f} ${on_stats['pnl']-off_stats['pnl']:>+9.2f}")
        lines.append(f"  {'Pips':>20} {off_stats['pips']:>+13.1f} {on_stats['pips']:>+13.1f} {on_stats['pips']-off_stats['pips']:>+9.1f}")
        lines.append(f"  {'Profit Factor':>20} {off_stats['pf']:>14.2f} {on_stats['pf']:>14.2f}")
        
        # Verdict
        delta_pnl = on_stats['pnl'] - off_stats['pnl']
        delta_wr = on_stats['wr'] - off_stats['wr']
        if delta_pnl > 0 and delta_wr >= 0:
            lines.append(f"\n  ✅ Patterns ON is BETTER: +${delta_pnl:.2f} P&L, +{delta_wr:.1f}% WR")
        elif delta_pnl > 0:
            lines.append(f"\n  ⚠️ Patterns ON has better P&L (+${delta_pnl:.2f}) but lower WR ({delta_wr:.1f}%)")
        elif delta_pnl < 0:
            lines.append(f"\n  ❌ Patterns ON is WORSE: ${delta_pnl:.2f} P&L, {delta_wr:.1f}% WR — DO NOT DEPLOY")
        else:
            lines.append(f"\n  ➖ No significant difference")
    
    # ============================================================
    # CANDLESTICK PATTERN ANALYSIS (from Patterns ON run)
    # ============================================================
    pattern_trades = [t for t in trades if t.candlestick_pattern]
    if pattern_trades:
        lines.append(f"\n{'─' * 50}")
        lines.append(f"  🕯️ CANDLESTICK PATTERN BREAKDOWN")
        lines.append(f"{'─' * 50}")
        
        # Group by pattern
        by_pattern = {}
        for t in pattern_trades:
            p = t.candlestick_pattern
            if p not in by_pattern:
                by_pattern[p] = {'trades': [], 'wins': 0, 'pnl': 0.0, 'pips': 0.0}
            by_pattern[p]['trades'].append(t)
            if t.profit_pips > 0:
                by_pattern[p]['wins'] += 1
            by_pattern[p]['pnl'] += t.profit_usd
            by_pattern[p]['pips'] += t.profit_pips
        
        lines.append(f"\n  Trades with patterns: {len(pattern_trades)} / {len(trades)} ({len(pattern_trades)/len(trades)*100:.0f}%)")
        lines.append(f"\n  {'Pattern':<25} {'Trades':>7} {'Wins':>5} {'WR%':>7} {'P&L $':>10} {'Pips':>8} {'Avg SR×':>8}")
        
        for p, d in sorted(by_pattern.items(), key=lambda x: -len(x[1]['trades'])):
            count = len(d['trades'])
            wr = d['wins'] / count * 100 if count > 0 else 0
            avg_sr_mult = np.mean([t.candlestick_sr_mult for t in d['trades']])
            lines.append(f"  {p:<25} {count:>7} {d['wins']:>5} {wr:>6.1f}% ${d['pnl']:>+9.2f} {d['pips']:>+7.1f} {avg_sr_mult:>7.2f}×")
        
        # S/R Proximity Impact Analysis
        sr_boosted = [t for t in pattern_trades if t.candlestick_sr_mult > 1.0]
        no_sr_boost = [t for t in pattern_trades if t.candlestick_sr_mult == 1.0]
        
        lines.append(f"\n  S/R Proximity Impact:")
        if sr_boosted:
            sr_wins = sum(1 for t in sr_boosted if t.profit_pips > 0)
            sr_wr = sr_wins / len(sr_boosted) * 100
            sr_pnl = sum(t.profit_usd for t in sr_boosted)
            lines.append(f"    WITH S/R boost (×>1.0):    {len(sr_boosted)} trades, WR {sr_wr:.1f}%, P&L ${sr_pnl:+.2f}")
        else:
            lines.append(f"    WITH S/R boost (×>1.0):    0 trades")
        
        if no_sr_boost:
            no_sr_wins = sum(1 for t in no_sr_boost if t.profit_pips > 0)
            no_sr_wr = no_sr_wins / len(no_sr_boost) * 100
            no_sr_pnl = sum(t.profit_usd for t in no_sr_boost)
            lines.append(f"    WITHOUT S/R boost (×1.0):  {len(no_sr_boost)} trades, WR {no_sr_wr:.1f}%, P&L ${no_sr_pnl:+.2f}")
        else:
            lines.append(f"    WITHOUT S/R boost (×1.0):  0 trades")
        
        if sr_boosted and no_sr_boost:
            sr_wr = sum(1 for t in sr_boosted if t.profit_pips > 0) / len(sr_boosted) * 100
            no_sr_wr = sum(1 for t in no_sr_boost if t.profit_pips > 0) / len(no_sr_boost) * 100
            if sr_wr > no_sr_wr:
                lines.append(f"    ✅ Patterns near S/R zones have HIGHER win rate (+{sr_wr - no_sr_wr:.1f}%)")
            elif sr_wr < no_sr_wr:
                lines.append(f"    ⚠️ Patterns near S/R zones have LOWER win rate ({sr_wr - no_sr_wr:.1f}%)")
            else:
                lines.append(f"    ➖ No difference in win rate")
    
    # ============================================================
    # FEB 16 VALIDATION
    # ============================================================
    feb16_decisions = getattr(run_backtest, '_feb16_decisions', [])
    if feb16_decisions:
        lines.append(f"\n{'─' * 50}")
        lines.append(f"  🎯 FEB 16 VALIDATION (04:00-09:00 UTC)")
        lines.append(f"{'─' * 50}")
        lines.append(f"  {'Time':>5} {'Decision':>12} {'Score':>6} {'Conf':>6} {'Scenario':>25} {'Tech':>5} {'ML':>5} {'Mom':>5} {'Vol':>10}")

        for d in feb16_decisions:
            lines.append(
                f"  {d['time']:>5} {d['decision']:>12} {d['score']:>6.1f} {d['confidence']:>6.1f} "
                f"{d['scenario']:>25} {d['tech']:>5.1f} {d['ml']:>5.1f} {d['momentum']:>5.1f} {d['vol_status']:>10}"
            )

        feb16_trades = [t for t in trades if t.entry_time and t.entry_time.date() == datetime(2026, 2, 16).date()
                        and 3 <= t.entry_time.hour <= 10]
        if feb16_trades:
            lines.append(f"\n  Trades opened on Feb 16 (04:00-10:00):")
            for t in feb16_trades:
                lines.append(f"    {t.entry_time.strftime('%H:%M')} {t.direction} @ {t.entry_price:.2f} → {t.close_price:.2f} ({t.close_reason}) P&L={t.profit_pips:+.1f} pips")
        else:
            lines.append(f"\n  ✅ No trades opened on Feb 16 04:00-10:00 (would NOT have opened those SELLs)")

    lines.append(f"\n{'=' * 70}")
    return "\n".join(lines)


# ============================================================================
# MAIN
# ============================================================================

def main():
    import argparse
    parser = argparse.ArgumentParser(description="XAUUSD Backtest Engine")
    parser.add_argument('--news-score', type=float, default=None,
                        help='Override news score (default 50).')
    parser.add_argument('--sensitivity', action='store_true',
                        help='Run sensitivity test with News=40/50/60')
    parser.add_argument('--start', type=str, default=None,
                        help='Override backtest start date (YYYY-MM-DD)')
    parser.add_argument('--end', type=str, default=None,
                        help='Override backtest end date (YYYY-MM-DD or YYYY-MM-DD HH:MM)')
    parser.add_argument('--models-dir', type=str, default=None,
                        help='Override models directory (e.g., models_v3_backup)')
    parser.add_argument('--weights', type=str, default=None,
                        help='Override BASE_WEIGHTS as JSON, e.g. {"technical":0.35,"ml":0.25,"momentum":0.20,"news":0.15,"calendar":0.05}')
    args = parser.parse_args()

    global NEUTRAL_NEWS, BT_NEWS_SCORE, BT_START, BT_END

    # Override weights if provided
    if args.weights:
        import json as _json
        custom_weights = _json.loads(args.weights)
        assert abs(sum(custom_weights.values()) - 1.0) < 0.01, f"Weights must sum to 1.0, got {sum(custom_weights.values())}"
        set_base_weights(custom_weights)
        print(f"⚖️ Custom weights: {custom_weights}")

    # Resolve models directory
    bt_models_dir = None
    if args.models_dir:
        bt_models_dir = os.path.join(ROOT_DIR, args.models_dir) if not os.path.isabs(args.models_dir) else args.models_dir
        if not os.path.isdir(bt_models_dir):
            print(f"❌ Models directory not found: {bt_models_dir}")
            return
        print(f"📁 Using models from: {bt_models_dir}")

    # Override date range if provided
    if args.start:
        BT_START = datetime.strptime(args.start, '%Y-%m-%d')
    if args.end:
        try:
            BT_END = datetime.strptime(args.end, '%Y-%m-%d %H:%M')
        except ValueError:
            BT_END = datetime.strptime(args.end, '%Y-%m-%d')

    print("=" * 60)
    print("  BACKTEST ENGINE — XAUUSD Trading Bot")
    print("=" * 60)

    # Connect MT5
    if not connect():
        return

    try:
        # Collect data
        data = collect_all_data()

        for key in ['h1', 'h4', 'm5']:
            if data[key].empty:
                print(f"❌ Missing {key} data. Aborting.")
                return

        if args.sensitivity:
            # Sensitivity test: run with multiple news scores
            print("\n" + "=" * 60)
            print("  SENSITIVITY TEST: News = 40 / 50 / 60")
            print("=" * 60)
            results = []
            for ns in [40.0, 50.0, 60.0]:
                BT_NEWS_SCORE = ns
                NEUTRAL_NEWS = _make_news_dict(ns)
                print(f"\n--- News Score = {ns} ---")
                trades, _ = run_backtest(data, disable_visual=False, models_dir=bt_models_dir)
                wins = [t for t in trades if t.profit_usd > 0]
                losses = [t for t in trades if t.profit_usd <= 0]
                total_pnl = sum(t.profit_usd for t in trades)
                gross_win = sum(t.profit_usd for t in wins) if wins else 0
                gross_loss = abs(sum(t.profit_usd for t in losses)) if losses else 1
                pf = gross_win / gross_loss if gross_loss > 0 else float('inf')
                wr = len(wins) / len(trades) * 100 if trades else 0
                results.append({'news': ns, 'trades': len(trades), 'wr': wr, 'pnl': total_pnl, 'pf': pf})
            print("\n" + "=" * 60)
            print("  SENSITIVITY RESULTS")
            print("=" * 60)
            print(f"  {'News':>6} {'Trades':>7} {'WR%':>7} {'P&L':>10} {'PF':>7}")
            for r in results:
                print(f"  {r['news']:>6.0f} {r['trades']:>7} {r['wr']:>6.1f}% ${r['pnl']:>+9.2f} {r['pf']:>7.2f}")
            return

        # Single run
        if args.news_score is not None:
            BT_NEWS_SCORE = args.news_score
            NEUTRAL_NEWS = _make_news_dict(args.news_score)

        print("\n" + "=" * 60)
        print(f"  RUNNING BACKTEST (news={BT_NEWS_SCORE:.0f})")
        print(f"  Period: {BT_START.strftime('%Y-%m-%d')} → {BT_END.strftime('%Y-%m-%d %H:%M')}")
        print("=" * 60)

        # Run A: Patterns OFF (baseline) — disable visual features
        print("\n--- Run A: Patterns OFF (baseline) ---")
        trades_patterns_off, pyr_stats_off = run_backtest(data, disable_visual=True, sr_enabled=True, sr_tp_adjust=True, models_dir=bt_models_dir)

        # Run B: Patterns ON with S/R enabled — test proximity scaling
        print("\n--- Run B: Patterns ON + S/R enabled ---")
        trades_patterns_on, pyr_stats_on = run_backtest(data, disable_visual=False, sr_enabled=True, sr_tp_adjust=True, models_dir=bt_models_dir)

        # Build pattern comparison data
        pattern_comparison = {
            'patterns_off': trades_patterns_off,
            'patterns_on': trades_patterns_on,
        }

        # Generate report (patterns ON as primary trades)
        report = generate_report(
            trades_patterns_on,
            pyramid_stats=pyr_stats_on,
            pattern_comparison=pattern_comparison,
        )
        print(report)

        # Save report
        timestamp = datetime.now().strftime('%Y%m%d_%H%M')
        report_path = os.path.join(ROOT_DIR, "data", f"backtest_report_{timestamp}.txt")
        with open(report_path, 'w', encoding='utf-8') as f:
            f.write(report)
        print(f"\n📄 Report saved: {report_path}")

        # Save trades CSV
        if trades_patterns_on:
            csv_path = os.path.join(ROOT_DIR, "data", f"backtest_trades_{timestamp}.csv")
            rows = []
            for t in trades_patterns_on:
                rows.append({
                    'ticket': t.ticket, 'direction': t.direction,
                    'entry_time': t.entry_time, 'entry_price': t.entry_price,
                    'sl': t.sl, 'tp': t.tp,
                    'close_time': t.close_time, 'close_price': t.close_price,
                    'close_reason': t.close_reason,
                    'profit_pips': round(t.profit_pips, 1),
                    'profit_usd': round(t.profit_usd, 2),
                    'duration_min': round(t.duration_minutes, 0),
                    'brain_score': t.brain_score, 'confidence': t.confidence,
                    'scenario': t.scenario,
                    'tech_score': t.tech_score, 'ml_score': t.ml_score,
                    'momentum_score': t.momentum_score,
                    'max_favorable_pips': t.max_favorable_pips,
                    'max_adverse_pips': t.max_adverse_pips,
                    'is_pyramid': t.is_pyramid,
                })
            pd.DataFrame(rows).to_csv(csv_path, index=False)
            print(f"📄 Trades CSV saved: {csv_path}")

    finally:
        mt5.shutdown()
        print("\n✅ MT5 disconnected. Backtest complete.")


if __name__ == "__main__":
    main()
