"""
Training Data Collection for XGBoost — XAUUSD Trading Bot
=========================================================
Collects data from multiple sources, aligns temporally in UTC,
calculates 34 features and saves dataset for training.

MT5 sources (H1): XAUUSD, XAGUSD, XTIUSD, US500
Yahoo sources (daily, forward-fill): DXY, VIX, Treasury 10Y

IMPORTANT: MT5 server time = UTC+2. We convert everything to UTC.
"""

import os
import sys
import numpy as np
import pandas as pd
import pandas_ta as ta
import yfinance as yf
from datetime import datetime, timedelta

# Add parent dir to path for config import
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import MetaTrader5 as mt5

# ============================================================================
# CONFIG
# ============================================================================
DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
os.makedirs(DATA_DIR, exist_ok=True)

MT5_ACCOUNT = 52704729
MT5_PASSWORD = "EnK2S8TUd&l$VG"
MT5_SERVER = "CapitalPointTrading-Demo"

# MT5 server offset: server_time = UTC + 2h
MT5_UTC_OFFSET_HOURS = 2

# Collection period
START_DATE = datetime(2023, 1, 1)
END_DATE = datetime(2026, 2, 18)

# Output
OUTPUT_FILE = os.path.join(DATA_DIR, "training_dataset.csv")


# ============================================================================
# PART 1: MT5 DATA COLLECTION
# ============================================================================

def connect_mt5():
    """Connect to MT5"""
    if not mt5.initialize():
        print(f"❌ MT5 init failed: {mt5.last_error()}")
        return False
    if not mt5.login(MT5_ACCOUNT, password=MT5_PASSWORD, server=MT5_SERVER):
        print(f"❌ MT5 login failed: {mt5.last_error()}")
        mt5.shutdown()
        return False
    print(f"✅ MT5 connected: {MT5_SERVER}")
    return True


def collect_mt5_h1(symbol: str, start: datetime, end: datetime) -> pd.DataFrame:
    """
    Collect H1 data from MT5 and convert to UTC.
    MT5 timestamps are Unix epoch (UTC), but the server candle times
    are in server timezone (UTC+2). We subtract the offset.
    """
    if not mt5.symbol_select(symbol, True):
        print(f"  ❌ Symbol {symbol} not available")
        return pd.DataFrame()

    rates = mt5.copy_rates_range(symbol, mt5.TIMEFRAME_H1, start, end)
    if rates is None or len(rates) == 0:
        print(f"  ❌ No data for {symbol}")
        return pd.DataFrame()

    df = pd.DataFrame(rates)
    # MT5 'time' is Unix timestamp. Convert to datetime.
    # MT5 candle open times are in server time (UTC+2).
    # Subtract offset to get UTC.
    df['datetime'] = pd.to_datetime(df['time'], unit='s') - pd.Timedelta(hours=MT5_UTC_OFFSET_HOURS)
    df = df.rename(columns={'tick_volume': 'volume'})
    df = df[['datetime', 'open', 'high', 'low', 'close', 'volume']].copy()
    df = df.sort_values('datetime').drop_duplicates(subset='datetime').reset_index(drop=True)

    print(f"  ✅ {symbol} H1: {len(df):,} bars | {df['datetime'].iloc[0]} → {df['datetime'].iloc[-1]} (UTC)")
    return df


def collect_mt5_m5(symbol: str, start: datetime, end: datetime) -> pd.DataFrame:
    """
    Collect M5 data from MT5 and convert to UTC.
    M5 data may be limited to ~1-2 years depending on broker.
    Collects in 60-day chunks to avoid MT5 limits.
    """
    if not mt5.symbol_select(symbol, True):
        print(f"  ❌ Symbol {symbol} not available")
        return pd.DataFrame()

    all_dfs = []
    chunk_start = start
    while chunk_start < end:
        chunk_end = min(chunk_start + timedelta(days=60), end)
        rates = mt5.copy_rates_range(symbol, mt5.TIMEFRAME_M5, chunk_start, chunk_end)
        if rates is not None and len(rates) > 0:
            chunk_df = pd.DataFrame(rates)
            all_dfs.append(chunk_df)
        chunk_start = chunk_end

    if not all_dfs:
        print(f"  ⚠️ {symbol} M5: no data available")
        return pd.DataFrame()

    df = pd.concat(all_dfs, ignore_index=True).drop_duplicates(subset='time')
    df['datetime'] = pd.to_datetime(df['time'], unit='s') - pd.Timedelta(hours=MT5_UTC_OFFSET_HOURS)
    df = df.rename(columns={'tick_volume': 'volume'})
    df = df[['datetime', 'open', 'high', 'low', 'close', 'volume']].copy()
    df = df.sort_values('datetime').reset_index(drop=True)

    print(f"  ✅ {symbol} M5: {len(df):,} bars | {df['datetime'].iloc[0]} → {df['datetime'].iloc[-1]} (UTC)")
    return df


def collect_mt5_h4(symbol: str, start: datetime, end: datetime) -> pd.DataFrame:
    """Collect H4 data from MT5 and convert to UTC."""
    if not mt5.symbol_select(symbol, True):
        print(f"  ❌ Symbol {symbol} not available")
        return pd.DataFrame()

    rates = mt5.copy_rates_range(symbol, mt5.TIMEFRAME_H4, start, end)
    if rates is None or len(rates) == 0:
        print(f"  ❌ No H4 data for {symbol}")
        return pd.DataFrame()

    df = pd.DataFrame(rates)
    df['datetime'] = pd.to_datetime(df['time'], unit='s') - pd.Timedelta(hours=MT5_UTC_OFFSET_HOURS)
    df = df.rename(columns={'tick_volume': 'volume'})
    df = df[['datetime', 'open', 'high', 'low', 'close', 'volume']].copy()
    df = df.sort_values('datetime').drop_duplicates(subset='datetime').reset_index(drop=True)

    print(f"  ✅ {symbol} H4: {len(df):,} bars | {df['datetime'].iloc[0]} → {df['datetime'].iloc[-1]} (UTC)")
    return df


def collect_mt5_d1(symbol: str, start: datetime, end: datetime) -> pd.DataFrame:
    """Collect D1 data from MT5 for multi-timeframe features."""
    if not mt5.symbol_select(symbol, True):
        return pd.DataFrame()

    rates = mt5.copy_rates_range(symbol, mt5.TIMEFRAME_D1, start, end)
    if rates is None or len(rates) == 0:
        return pd.DataFrame()

    df = pd.DataFrame(rates)
    df['datetime'] = pd.to_datetime(df['time'], unit='s') - pd.Timedelta(hours=MT5_UTC_OFFSET_HOURS)
    df['date'] = df['datetime'].dt.date
    df = df.rename(columns={'tick_volume': 'volume'})
    df = df[['date', 'datetime', 'open', 'high', 'low', 'close', 'volume']].copy()
    df = df.sort_values('datetime').drop_duplicates(subset='date').reset_index(drop=True)

    print(f"  ✅ {symbol} D1: {len(df):,} bars | {df['date'].iloc[0]} → {df['date'].iloc[-1]} (UTC)")
    return df


# ============================================================================
# PART 2: YAHOO FINANCE DATA COLLECTION
# ============================================================================

def collect_yahoo_daily(symbol: str, name: str, start: datetime, end: datetime) -> pd.DataFrame:
    """
    Collect daily data from Yahoo Finance.
    Yahoo returns data in UTC by default.
    """
    try:
        ticker = yf.Ticker(symbol)
        hist = ticker.history(start=start.strftime('%Y-%m-%d'),
                              end=(end + timedelta(days=1)).strftime('%Y-%m-%d'))
        if hist.empty:
            print(f"  ❌ Yahoo {name} ({symbol}): no data")
            return pd.DataFrame()

        hist = hist.reset_index()
        hist['date'] = pd.to_datetime(hist['Date']).dt.date
        hist = hist[['date', 'Close']].rename(columns={'Close': f'{name}_close'})
        hist = hist.sort_values('date').drop_duplicates(subset='date').reset_index(drop=True)

        print(f"  ✅ Yahoo {name}: {len(hist):,} days | {hist['date'].iloc[0]} → {hist['date'].iloc[-1]} (UTC)")
        return hist

    except Exception as e:
        print(f"  ❌ Yahoo {name} ({symbol}): {e}")
        return pd.DataFrame()


def collect_dxy_daily(start: datetime, end: datetime) -> pd.DataFrame:
    """Try multiple DXY symbols."""
    for sym in ["DX-Y.NYB", "DX=F"]:
        df = collect_yahoo_daily(sym, "dxy", start, end)
        if not df.empty:
            return df
    print("  ⚠️ DXY: all symbols failed")
    return pd.DataFrame()


# ============================================================================
# PART 3: MERGE & ALIGN
# ============================================================================

def aggregate_m5_to_h1(xau_h1: pd.DataFrame, xau_m5: pd.DataFrame) -> pd.DataFrame:
    """
    Aggregate M5 features onto H1 index using lookback-based approach.
    For each H1 bar, compute features from the last N M5 candles
    (not "M5 candles within this hour"), ensuring train/live consistency.
    """
    if xau_m5.empty:
        print("  ⚠️ No M5 data — M5 features will be NaN (filled with 0 later)")
        for col in ['momentum_M15', 'volume_spike_M5', 'consecutive_candles_M15', 'price_vs_vwap_intraday',
                    'price_change_M30']:
            xau_h1[col] = np.nan
        return xau_h1

    m5 = xau_m5.sort_values('datetime').reset_index(drop=True)

    # momentum_M15: price change % of last 3 M5 candles
    m5['momentum_M15'] = m5['close'].pct_change(3) * 100

    # price_change_M30: % change over last 6 M5 candles (30 min)
    m5['price_change_M30'] = m5['close'].pct_change(6) * 100

    # volume_spike_M5: volume of last 3 M5 candles / avg of last 20 M5 candles
    vol_sum_3 = m5['volume'].rolling(3).sum()
    vol_avg_20 = m5['volume'].rolling(20).mean()
    m5['volume_spike_M5'] = vol_sum_3 / (3 * vol_avg_20.replace(0, np.nan))

    # consecutive_candles_M15: count consecutive M5 candles in same direction
    # Positive = bullish streak, negative = bearish streak
    direction = np.sign(m5['close'].values - m5['open'].values)
    consecutive = np.zeros(len(m5), dtype=int)
    for i in range(1, len(m5)):
        if direction[i] == 0:
            consecutive[i] = 0
        elif direction[i] == direction[i - 1]:
            consecutive[i] = consecutive[i - 1] + int(direction[i])
        else:
            consecutive[i] = int(direction[i])
    m5['consecutive_candles_M15'] = consecutive

    # price_vs_vwap_intraday: VWAP from start of day to current M5 bar
    m5['date'] = m5['datetime'].dt.date
    m5['typical_price'] = (m5['high'] + m5['low'] + m5['close']) / 3
    m5['tp_vol'] = m5['typical_price'] * m5['volume']
    m5['cum_tp_vol'] = m5.groupby('date')['tp_vol'].cumsum()
    m5['cum_vol'] = m5.groupby('date')['volume'].cumsum()
    m5['vwap'] = m5['cum_tp_vol'] / m5['cum_vol'].replace(0, np.nan)
    m5['price_vs_vwap_intraday'] = (m5['close'] - m5['vwap']) / m5['close'] * 100

    # Map M5 features to H1: for each H1 datetime, take the last M5 bar <= H1 time
    m5_features = m5[['datetime', 'momentum_M15', 'volume_spike_M5',
                       'consecutive_candles_M15', 'price_vs_vwap_intraday',
                       'price_change_M30']].copy()

    xau_h1 = xau_h1.sort_values('datetime')
    m5_features = m5_features.sort_values('datetime')
    xau_h1 = pd.merge_asof(xau_h1, m5_features, on='datetime', direction='backward')

    m5_count = xau_h1['momentum_M15'].notna().sum()
    print(f"  ✅ M5 features merged: {m5_count:,}/{len(xau_h1):,} H1 bars have M5 data")
    return xau_h1


def merge_h4_to_h1(xau_h1: pd.DataFrame, xau_h4: pd.DataFrame) -> pd.DataFrame:
    """
    Merge H4 features onto H1 index.
    Calculate H4 indicators on H4 data, then map to H1 via merge_asof.
    """
    if xau_h4.empty:
        print("  ⚠️ No H4 data — H4 features will be NaN (filled with 0 later)")
        for col in ['rsi_H4', 'price_change_H4', 'dist_ema21_H4']:
            xau_h1[col] = np.nan
        return xau_h1

    h4 = xau_h4.sort_values('datetime').reset_index(drop=True)

    # RSI H4
    h4['rsi_H4'] = ta.rsi(h4['close'], length=14)

    # Price change H4 (1-bar = 4 hours)
    h4['price_change_H4'] = h4['close'].pct_change(1) * 100

    # EMA21 on H4
    h4['ema21_h4'] = ta.ema(h4['close'], length=21)
    h4['dist_ema21_H4'] = (h4['close'] - h4['ema21_h4']) / h4['close'] * 100

    h4_features = h4[['datetime', 'rsi_H4', 'price_change_H4', 'dist_ema21_H4']].copy()

    xau_h1 = xau_h1.sort_values('datetime')
    h4_features = h4_features.sort_values('datetime')
    xau_h1 = pd.merge_asof(xau_h1, h4_features, on='datetime', direction='backward')

    h4_count = xau_h1['rsi_H4'].notna().sum()
    print(f"  ✅ H4 features merged: {h4_count:,}/{len(xau_h1):,} H1 bars have H4 data")
    return xau_h1


def merge_all_data(xau_h1: pd.DataFrame, xag_h1: pd.DataFrame,
                   oil_h1: pd.DataFrame, sp500_h1: pd.DataFrame,
                   xau_d1: pd.DataFrame,
                   dxy_daily: pd.DataFrame, vix_daily: pd.DataFrame,
                   yields_daily: pd.DataFrame,
                   xau_m5: pd.DataFrame = None,
                   xau_h4: pd.DataFrame = None) -> pd.DataFrame:
    """
    Merge all data sources into a single DataFrame aligned on XAUUSD H1 datetime.
    Daily data is forward-filled to H1 bars.
    M5 and H4 features are computed on their native timeframes then mapped to H1.
    """
    if xau_m5 is None:
        xau_m5 = pd.DataFrame()
    if xau_h4 is None:
        xau_h4 = pd.DataFrame()

    df = xau_h1.copy()
    df['date'] = df['datetime'].dt.date

    # --- Merge H1 assets (XAGUSD, XTIUSD, US500) ---
    for asset_df, prefix in [(xag_h1, 'xag'), (oil_h1, 'oil'), (sp500_h1, 'sp500')]:
        if not asset_df.empty:
            asset = asset_df[['datetime', 'close']].rename(columns={'close': f'{prefix}_close'})
            df = pd.merge(df, asset, on='datetime', how='left')
            # Forward-fill gaps (weekends, missing bars)
            df[f'{prefix}_close'] = df[f'{prefix}_close'].ffill()
        else:
            df[f'{prefix}_close'] = np.nan

    # --- Merge daily data (DXY, VIX, Yields) via date ---
    for daily_df in [dxy_daily, vix_daily, yields_daily]:
        if not daily_df.empty:
            df = pd.merge(df, daily_df, on='date', how='left')

    # Forward-fill daily data to H1 bars
    for col in ['dxy_close', 'vix_close', 'yields_close']:
        if col in df.columns:
            df[col] = df[col].ffill()

    # --- Merge D1 data for multi-timeframe features ---
    if not xau_d1.empty:
        # Calculate EMA50 on D1
        xau_d1_feat = xau_d1[['date', 'close', 'high', 'low']].copy()
        xau_d1_feat['ema50_d1'] = ta.ema(xau_d1_feat['close'], length=50)
        # ATR D1 (14-period)
        xau_d1_feat['atr_d1'] = ta.atr(xau_d1_feat['high'], xau_d1_feat['low'],
                                         xau_d1_feat['close'], length=14)
        # Weekly price change (5 trading days)
        xau_d1_feat['close_5d_ago'] = xau_d1_feat['close'].shift(5)

        d1_merge = xau_d1_feat[['date', 'ema50_d1', 'atr_d1', 'close_5d_ago']].copy()
        df = pd.merge(df, d1_merge, on='date', how='left')
        for col in ['ema50_d1', 'atr_d1', 'close_5d_ago']:
            df[col] = df[col].ffill()

    # --- Merge M5 features (aggregated to H1) ---
    df = aggregate_m5_to_h1(df, xau_m5)

    # --- Merge H4 features ---
    df = merge_h4_to_h1(df, xau_h4)

    print(f"\n📊 Merged dataset: {len(df):,} rows, {len(df.columns)} columns")
    return df


# ============================================================================
# PART 4: FEATURE ENGINEERING — 41 FEATURES
# ============================================================================

def calculate_technical_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Calculate XAUUSD technical indicators."""
    # EMAs
    df['ema_9'] = ta.ema(df['close'], length=9)
    df['ema_21'] = ta.ema(df['close'], length=21)
    df['ema_50'] = ta.ema(df['close'], length=50)

    # RSI
    df['rsi_14'] = ta.rsi(df['close'], length=14)

    # MACD
    macd = ta.macd(df['close'], fast=12, slow=26, signal=9)
    df['macd'] = macd['MACD_12_26_9']
    df['macd_signal'] = macd['MACDs_12_26_9']
    df['macd_hist'] = macd['MACDh_12_26_9']

    # Bollinger Bands
    bbands = ta.bbands(df['close'], length=20, std=2)
    df['bb_upper'] = bbands['BBU_20_2.0']
    df['bb_middle'] = bbands['BBM_20_2.0']
    df['bb_lower'] = bbands['BBL_20_2.0']

    # ATR H1 (for atr_ratio)
    df['atr_h1'] = ta.atr(df['high'], df['low'], df['close'], length=14)

    return df


def engineer_features(df: pd.DataFrame) -> pd.DataFrame:
    """
    Engineer all 34 features. ZERO absolute prices — only returns %, distances %, ratios.
    """
    # =============================================
    # GROUP 1: Technical (14 features)
    # =============================================
    # Temporal
    df['hour'] = df['datetime'].dt.hour
    df['day_of_week'] = df['datetime'].dt.dayofweek

    # Price changes (%)
    df['price_change_1h'] = df['close'].pct_change(1) * 100
    df['price_change_4h'] = df['close'].pct_change(4) * 100
    df['price_change_24h'] = df['close'].pct_change(24) * 100

    # Volatility (range %)
    df['volatility_4h'] = (df['high'].rolling(4).max() - df['low'].rolling(4).min()) / df['close'] * 100
    df['volatility_24h'] = (df['high'].rolling(24).max() - df['low'].rolling(24).min()) / df['close'] * 100

    # Distance from EMAs (%)
    df['dist_ema9'] = (df['close'] - df['ema_9']) / df['close'] * 100
    df['dist_ema21'] = (df['close'] - df['ema_21']) / df['close'] * 100
    df['dist_ema50'] = (df['close'] - df['ema_50']) / df['close'] * 100

    # Bollinger position (0-100)
    bb_range = df['bb_upper'] - df['bb_lower']
    df['bb_position'] = np.where(bb_range > 0, (df['close'] - df['bb_lower']) / bb_range * 100, 50)

    # Momentum
    df['macd_momentum'] = df['macd_hist'].diff()
    df['rsi_momentum'] = df['rsi_14'].diff()

    # =============================================
    # GROUP 2: Macro (8 features)
    # =============================================
    # DXY
    if 'dxy_close' in df.columns:
        df['dxy_change_1d'] = df.groupby('date')['dxy_close'].transform('first')
        df['dxy_change_1d'] = df['dxy_change_1d'].pct_change() * 100
        df['dxy_change_1d'] = df.groupby('date')['dxy_change_1d'].transform('first')
        df['dxy_change_1d'] = df['dxy_change_1d'].ffill()

        # DXY level: z-score rolling 60 trading days (~1440 H1 bars)
        dxy_daily = df.groupby('date')['dxy_close'].first()
        dxy_mean = dxy_daily.rolling(60, min_periods=20).mean()
        dxy_std = dxy_daily.rolling(60, min_periods=20).std()
        dxy_zscore = (dxy_daily - dxy_mean) / dxy_std.replace(0, np.nan)
        dxy_zscore_map = dxy_zscore.to_dict()
        df['dxy_level'] = df['date'].map(dxy_zscore_map)
        df['dxy_level'] = df['dxy_level'].ffill()
    else:
        df['dxy_change_1d'] = 0.0
        df['dxy_level'] = 0.0

    # Yields 10Y
    if 'yields_close' in df.columns:
        yields_daily = df.groupby('date')['yields_close'].first()
        yields_change = yields_daily.pct_change() * 100
        yields_change_map = yields_change.to_dict()
        df['yields_10y_change'] = df['date'].map(yields_change_map)
        df['yields_10y_change'] = df['yields_10y_change'].ffill()
    else:
        df['yields_10y_change'] = 0.0

    # VIX
    if 'vix_close' in df.columns:
        vix_daily = df.groupby('date')['vix_close'].first()
        vix_change = vix_daily.pct_change() * 100
        vix_change_map = vix_change.to_dict()
        df['vix_level'] = df['vix_close']
        df['vix_change'] = df['date'].map(vix_change_map)
        df['vix_change'] = df['vix_change'].ffill()
    else:
        df['vix_level'] = 0.0
        df['vix_change'] = 0.0

    # XAG changes (%)
    if 'xag_close' in df.columns:
        df['xag_change_1h'] = df['xag_close'].pct_change(1) * 100
        df['xag_change_4h'] = df['xag_close'].pct_change(4) * 100
    else:
        df['xag_change_1h'] = 0.0
        df['xag_change_4h'] = 0.0

    # S&P500 daily change
    if 'sp500_close' in df.columns:
        sp_daily = df.groupby('date')['sp500_close'].first()
        sp_change = sp_daily.pct_change() * 100
        sp_change_map = sp_change.to_dict()
        df['sp500_change_1d'] = df['date'].map(sp_change_map)
        df['sp500_change_1d'] = df['sp500_change_1d'].ffill()
    else:
        df['sp500_change_1d'] = 0.0

    # =============================================
    # GROUP 3: Session/Time (3 features)
    # =============================================
    df['session'] = df['hour'].apply(lambda h: 0 if 0 <= h < 8 else (1 if 8 <= h < 14 else (2 if 14 <= h < 21 else 3)))
    df['is_london_open'] = df['hour'].apply(lambda h: 1 if h in (7, 8) else 0)
    df['is_ny_open'] = df['hour'].apply(lambda h: 1 if h in (13, 14) else 0)

    # =============================================
    # GROUP 4: Multi-Timeframe (3 features)
    # =============================================
    # Distance to D1 EMA50 (%)
    if 'ema50_d1' in df.columns:
        df['price_vs_ema50_D1'] = (df['close'] - df['ema50_d1']) / df['close'] * 100
    else:
        df['price_vs_ema50_D1'] = 0.0

    # Weekly price change (%) — using D1 close from 5 days ago
    if 'close_5d_ago' in df.columns:
        df['price_change_1W'] = (df['close'] - df['close_5d_ago']) / df['close_5d_ago'] * 100
    else:
        df['price_change_1W'] = df['close'].pct_change(24 * 5) * 100  # fallback: 120 H1 bars

    # ATR ratio H1/D1
    if 'atr_d1' in df.columns and 'atr_h1' in df.columns:
        df['atr_ratio_H1_vs_D1'] = df['atr_h1'] / df['atr_d1'].replace(0, np.nan)
    else:
        df['atr_ratio_H1_vs_D1'] = 0.0

    # =============================================
    # GROUP 5: Lagged (4 features)
    # =============================================
    df['gold_return_lag1'] = df['close'].pct_change(1).shift(1) * 100
    df['gold_return_lag4'] = df['close'].pct_change(4).shift(1) * 100

    if 'dxy_change_1d' in df.columns:
        df['dxy_change_lag1'] = df['dxy_change_1d'].shift(24)  # previous day (24 H1 bars)
    else:
        df['dxy_change_lag1'] = 0.0

    if 'vix_change' in df.columns:
        df['vix_change_lag1'] = df['vix_change'].shift(24)
    else:
        df['vix_change_lag1'] = 0.0

    # =============================================
    # GROUP 7: M5/M15 Microstructure (4 features)
    # =============================================
    # These are already computed in aggregate_m5_to_h1() and merged.
    # Just ensure they exist (will be NaN for old data without M5).
    for col in ['momentum_M15', 'volume_spike_M5', 'consecutive_candles_M15', 'price_vs_vwap_intraday',
                'price_change_M30']:
        if col not in df.columns:
            df[col] = np.nan

    # =============================================
    # GROUP 8: H4 Multi-Timeframe (3 features)
    # =============================================
    # These are already computed in merge_h4_to_h1() and merged.
    for col in ['rsi_H4', 'price_change_H4', 'dist_ema21_H4']:
        if col not in df.columns:
            df[col] = np.nan

    # =============================================
    # GROUP 6: Cross-Asset (2 features)
    # =============================================
    # XAG/XAU ratio — z-score rolling 60 days
    if 'xag_close' in df.columns:
        ratio = df['xag_close'] / df['close']
        ratio_mean = ratio.rolling(24 * 60, min_periods=24 * 20).mean()  # 60 days of H1 bars
        ratio_std = ratio.rolling(24 * 60, min_periods=24 * 20).std()
        df['xag_xau_ratio'] = (ratio - ratio_mean) / ratio_std.replace(0, np.nan)
    else:
        df['xag_xau_ratio'] = 0.0

    # Oil daily change
    if 'oil_close' in df.columns:
        oil_daily = df.groupby('date')['oil_close'].first()
        oil_change = oil_daily.pct_change() * 100
        oil_change_map = oil_change.to_dict()
        df['oil_change_1d'] = df['date'].map(oil_change_map)
        df['oil_change_1d'] = df['oil_change_1d'].ffill()
    else:
        df['oil_change_1d'] = 0.0

    # =============================================
    # GROUP 9: Sentiment, Regime & Interactions (5 features)
    # =============================================
    # Sentiment proxy: combines DXY + VIX + Yields into 0-100 score
    # In live, replaced by real news_score from GPT (same 0-100 range)
    dxy_ch = df['dxy_change_1d'].fillna(0)
    vix_ch = df['vix_change'].fillna(0) if 'vix_change' in df.columns else 0
    yields_ch = df['yields_10y_change'].fillna(0)
    df['sentiment_proxy'] = np.clip(50 - dxy_ch * 10 + yields_ch * 5 - vix_ch * 2, 0, 100)

    # Regime detection: 0=ranging, 1=trending, 2=volatile
    abs_ema50_d1 = df['price_vs_ema50_D1'].abs() if 'price_vs_ema50_D1' in df.columns else 0
    atr_r = df['atr_ratio_H1_vs_D1'] if 'atr_ratio_H1_vs_D1' in df.columns else 0
    vix_val = df['vix_level'] if 'vix_level' in df.columns else 0
    # Default: ranging (0)
    df['regime'] = 0
    # Trending: price far from D1 EMA50
    df.loc[abs_ema50_d1 > 2.0, 'regime'] = 1
    # Volatile: VIX high or ATR ratio high (overrides trending)
    df.loc[(vix_val > 25) | (atr_r > 0.25), 'regime'] = 2

    # Feature interactions
    df['dxy_x_vix'] = df['dxy_change_1d'].fillna(0) * df['vix_level'].fillna(17)
    df['momentum_x_volume'] = df.get('price_change_H4', pd.Series(0, index=df.index)).fillna(0) * \
                               df.get('volume_spike_M5', pd.Series(1, index=df.index)).fillna(1)
    df['trend_x_session'] = df.get('dist_ema21_H4', pd.Series(0, index=df.index)).fillna(0) * \
                             df['is_ny_open']

    return df


# ============================================================================
# PART 5: VALIDATION
# ============================================================================

FEATURE_COLUMNS = [
    # Group 1: Technical (14)
    'rsi_14', 'hour', 'day_of_week',
    'price_change_1h', 'price_change_4h', 'price_change_24h',
    'volatility_4h', 'volatility_24h',
    'dist_ema9', 'dist_ema21', 'dist_ema50',
    'bb_position', 'macd_momentum', 'rsi_momentum',
    # Group 2: Macro (8)
    'dxy_change_1d', 'dxy_level',
    'yields_10y_change', 'vix_level', 'vix_change',
    'xag_change_1h', 'xag_change_4h', 'sp500_change_1d',
    # Group 3: Session (3)
    'session', 'is_london_open', 'is_ny_open',
    # Group 4: Multi-Timeframe (3)
    'price_vs_ema50_D1', 'price_change_1W', 'atr_ratio_H1_vs_D1',
    # Group 5: Lagged (4)
    'gold_return_lag1', 'gold_return_lag4', 'dxy_change_lag1', 'vix_change_lag1',
    # Group 6: Cross-Asset (2)
    'xag_xau_ratio', 'oil_change_1d',
    # Group 7: M5/M15 Microstructure (5)
    'momentum_M15', 'volume_spike_M5', 'consecutive_candles_M15', 'price_vs_vwap_intraday',
    'price_change_M30',
    # Group 8: H4 Multi-Timeframe (3)
    'rsi_H4', 'price_change_H4', 'dist_ema21_H4',
    # Group 9: Sentiment, Regime & Interactions (5)
    'sentiment_proxy', 'regime',
    'dxy_x_vix', 'momentum_x_volume', 'trend_x_session',
]

# Columns that are absolute prices (MUST NOT be in features)
ABSOLUTE_PRICE_COLS = [
    'open', 'high', 'low', 'close', 'volume',
    'ema_9', 'ema_21', 'ema_50', 'macd', 'macd_signal', 'macd_hist',
    'bb_upper', 'bb_middle', 'bb_lower', 'atr_h1',
    'xag_close', 'oil_close', 'sp500_close',
    'dxy_close', 'vix_close', 'yields_close',
    'ema50_d1', 'atr_d1', 'close_5d_ago',
]


def validate_dataset(df: pd.DataFrame) -> bool:
    """Validate the final dataset."""
    print(f"\n{'='*60}")
    print("🔍 DATASET VALIDATION")
    print(f"{'='*60}")

    ok = True

    # Check all features present
    missing = [f for f in FEATURE_COLUMNS if f not in df.columns]
    if missing:
        print(f"  ❌ Missing features: {missing}")
        ok = False
    else:
        print(f"  ✅ All {len(FEATURE_COLUMNS)} features present")

    # Check NO absolute prices in feature columns
    leaked = [f for f in FEATURE_COLUMNS if f in ABSOLUTE_PRICE_COLS]
    if leaked:
        print(f"  ❌ ABSOLUTE PRICES in features: {leaked}")
        ok = False
    else:
        print(f"  ✅ Zero absolute prices in features")

    # Check NaN
    feat_df = df[FEATURE_COLUMNS]
    nan_counts = feat_df.isna().sum()
    nan_pct = (nan_counts / len(df) * 100).round(1)
    high_nan = nan_pct[nan_pct > 10]
    if len(high_nan) > 0:
        print(f"  ⚠️ Features with >10% NaN:")
        for col, pct in high_nan.items():
            print(f"      {col}: {pct}% ({nan_counts[col]:,} rows)")
    else:
        print(f"  ✅ No features with >10% NaN")

    total_nan = nan_counts.sum()
    total_cells = len(df) * len(FEATURE_COLUMNS)
    print(f"  📊 NaN total: {total_nan:,} / {total_cells:,} ({total_nan/total_cells*100:.2f}%)")

    # Check datetime is UTC
    print(f"  📊 Datetime range: {df['datetime'].iloc[0]} → {df['datetime'].iloc[-1]} (UTC)")
    print(f"  📊 Total rows: {len(df):,}")

    return ok


def show_sample(df: pd.DataFrame):
    """Show sample and descriptive statistics."""
    print(f"\n{'='*60}")
    print("📋 SAMPLE (first 20 rows, features only)")
    print(f"{'='*60}")

    pd.set_option('display.max_columns', 10)
    pd.set_option('display.width', 200)
    pd.set_option('display.float_format', '{:.4f}'.format)

    sample = df[['datetime'] + FEATURE_COLUMNS].head(20)
    # Only show first few and last few columns for readability
    print(sample.to_string(max_cols=10))

    print(f"\n{'='*60}")
    print(f"📊 DESCRIPTIVE STATISTICS ({len(FEATURE_COLUMNS)} features)")
    print(f"{'='*60}")

    desc = df[FEATURE_COLUMNS].describe().T
    desc['nan_count'] = df[FEATURE_COLUMNS].isna().sum()
    desc['nan_pct'] = (desc['nan_count'] / len(df) * 100).round(2)
    print(desc[['count', 'mean', 'std', 'min', '25%', '50%', '75%', 'max', 'nan_count', 'nan_pct']].to_string())


# ============================================================================
# MAIN
# ============================================================================

def main():
    print("=" * 70)
    print("📦 TRAINING DATA COLLECTION FOR XGBOOST")
    print("=" * 70)
    print(f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Period: {START_DATE.date()} → {END_DATE.date()}")
    print(f"MT5 server timezone: UTC+{MT5_UTC_OFFSET_HOURS}")
    print(f"Output: {OUTPUT_FILE}")
    print()

    # --- Step 1: Connect MT5 ---
    print("\n" + "─" * 70)
    print("📡 STEP 1: Connect to MT5")
    print("─" * 70)
    if not connect_mt5():
        return

    # --- Step 2: Collect MT5 H1 data ---
    print("\n" + "─" * 70)
    print("📊 STEP 2: Collect H1 data from MT5")
    print("─" * 70)
    xau_h1 = collect_mt5_h1("XAUUSD", START_DATE, END_DATE)
    xag_h1 = collect_mt5_h1("XAGUSD", START_DATE, END_DATE)
    oil_h1 = collect_mt5_h1("XTIUSD", START_DATE, END_DATE)
    sp500_h1 = collect_mt5_h1("US500", START_DATE, END_DATE)

    # --- Step 2b: Collect MT5 M5 data (microstructure features) ---
    print("\n" + "─" * 70)
    print("📊 STEP 2b: Collect M5 data from MT5 (microstructure)")
    print("─" * 70)
    xau_m5 = collect_mt5_m5("XAUUSD", START_DATE, END_DATE)

    # --- Step 2c: Collect MT5 H4 data (medium-term trend) ---
    print("\n" + "─" * 70)
    print("📊 STEP 2c: Collect H4 data from MT5 (medium-term)")
    print("─" * 70)
    h4_start = START_DATE - timedelta(days=60)  # Extra for EMA21 warmup
    xau_h4 = collect_mt5_h4("XAUUSD", h4_start, END_DATE)

    # --- Step 3: Collect MT5 D1 data (for multi-timeframe features) ---
    print("\n" + "─" * 70)
    print("📊 STEP 3: Collect D1 data from MT5 (multi-timeframe)")
    print("─" * 70)
    # Start earlier to have enough D1 data for EMA50 warmup
    d1_start = START_DATE - timedelta(days=120)
    xau_d1 = collect_mt5_d1("XAUUSD", d1_start, END_DATE)

    mt5.shutdown()
    print("  ✅ MT5 disconnected")

    # --- Step 4: Collect Yahoo daily data ---
    print("\n" + "─" * 70)
    print("📊 STEP 4: Collect daily data from Yahoo Finance")
    print("─" * 70)
    # Start earlier for rolling calculations warmup
    yahoo_start = START_DATE - timedelta(days=120)
    dxy_daily = collect_dxy_daily(yahoo_start, END_DATE)
    vix_daily = collect_yahoo_daily("^VIX", "vix", yahoo_start, END_DATE)
    yields_daily = collect_yahoo_daily("^TNX", "yields", yahoo_start, END_DATE)

    if xau_h1.empty:
        print("\n❌ XAUUSD H1 data is empty. Cannot proceed.")
        return

    # --- Step 5: Merge all data ---
    print("\n" + "─" * 70)
    print("🔗 STEP 5: Merge and temporal alignment")
    print("─" * 70)
    df = merge_all_data(xau_h1, xag_h1, oil_h1, sp500_h1, xau_d1,
                        dxy_daily, vix_daily, yields_daily,
                        xau_m5=xau_m5, xau_h4=xau_h4)

    # --- Step 6: Calculate technical indicators ---
    print("\n" + "─" * 70)
    print("📐 STEP 6: Calculate technical indicators")
    print("─" * 70)
    df = calculate_technical_indicators(df)
    print(f"  ✅ Indicators calculated")

    # --- Step 7: Engineer all 46 features ---
    print("\n" + "─" * 70)
    print("🔧 STEP 7: Feature engineering (46 features)")
    print("─" * 70)
    df = engineer_features(df)
    print(f"  ✅ {len(FEATURE_COLUMNS)} features created")

    # --- Step 8: Validate ---
    valid = validate_dataset(df)

    # --- Step 9: Save ---
    print(f"\n{'─'*70}")
    print("💾 STEP 9: Save dataset")
    print(f"{'─'*70}")

    # Keep datetime + OHLCV (for label creation) + all features
    save_cols = ['datetime', 'close'] + FEATURE_COLUMNS
    df_save = df[save_cols].copy()

    # M5/H4/interaction features may be NaN for old data — fill with 0
    m5_h4_cols = ['momentum_M15', 'volume_spike_M5', 'consecutive_candles_M15',
                  'price_vs_vwap_intraday', 'price_change_M30',
                  'rsi_H4', 'price_change_H4', 'dist_ema21_H4',
                  'momentum_x_volume', 'trend_x_session']
    for col in m5_h4_cols:
        if col in df_save.columns:
            n_nan = df_save[col].isna().sum()
            df_save[col] = df_save[col].fillna(0.0)
            if n_nan > 0:
                print(f"  ℹ️ {col}: filled {n_nan:,} NaN with 0 (no M5/H4 data for old rows)")

    # Drop rows where core features (non-M5/H4) are NaN (warmup period)
    core_features = [f for f in FEATURE_COLUMNS if f not in m5_h4_cols]
    initial_len = len(df_save)
    df_save = df_save.dropna(subset=core_features)
    dropped = initial_len - len(df_save)
    print(f"  Dropped {dropped:,} rows with NaN in core features (warmup period)")
    print(f"  Final dataset: {len(df_save):,} rows")

    df_save.to_csv(OUTPUT_FILE, index=False)
    size_mb = os.path.getsize(OUTPUT_FILE) / (1024 * 1024)
    print(f"  ✅ Saved: {OUTPUT_FILE} ({size_mb:.1f} MB)")

    # --- Step 10: Show sample ---
    show_sample(df_save)

    print(f"\n{'='*70}")
    print("✅ COLLECTION COMPLETE!")
    print(f"{'='*70}")


if __name__ == "__main__":
    main()
