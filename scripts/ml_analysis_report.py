"""
ML v2 Analysis Report — Post-training validation with rank-based calibration.
"""

import os
import sys
import json
import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")
MODELS_DIR = os.path.join(BASE_DIR, "models")


def rank_calibrate(raw_proba, p10, p50, p90):
    """Rank-based calibration: P10->25, P50->50, P90->75."""
    if raw_proba <= p10:
        score = 25.0 * (raw_proba / p10) if p10 > 0 else 10.0
    elif raw_proba <= p50:
        frac = (raw_proba - p10) / max(p50 - p10, 1e-6)
        score = 25.0 + frac * 25.0
    elif raw_proba <= p90:
        frac = (raw_proba - p50) / max(p90 - p50, 1e-6)
        score = 50.0 + frac * 25.0
    else:
        extra = (raw_proba - p90) / max(1.0 - p90, 1e-6)
        score = 75.0 + extra * 15.0
    return max(10.0, min(90.0, score))


def per_fold_auc():
    """Per-fold AUC for v2 model with 20 features."""
    print("=" * 70)
    print("PER-FOLD AUC (13 walk-forward folds, v2 model)")
    print("=" * 70)

    from scripts.train_xgboost import load_dataset, create_labels, walk_forward_split
    import xgboost as xgb
    from sklearn.metrics import roc_auc_score, accuracy_score

    df = load_dataset()
    labels = create_labels(df, 0.15)

    with open(os.path.join(MODELS_DIR, "xgboost_features.json")) as f:
        features = json.load(f)
    with open(os.path.join(MODELS_DIR, "xgboost_config.json")) as f:
        cfg = json.load(f)
    params = cfg['xgboost_params']

    folds = walk_forward_split(df)
    aucs = []

    print(f"\n  {'Fold':>4} {'Period':>20} {'Train':>7} {'Test':>6} {'AUC':>7} {'Acc':>7} {'Verdict':>10}")
    print("  " + "-" * 70)

    for i, (train_idx, test_idx) in enumerate(folds):
        X_train = df.loc[train_idx, features].values
        y_train = labels.iloc[train_idx].values
        X_test = df.loc[test_idx, features].values
        y_test = labels.iloc[test_idx].values

        train_mask = ~np.isnan(y_train)
        test_mask = ~np.isnan(y_test)
        X_train, y_train = X_train[train_mask], y_train[train_mask]
        X_test, y_test = X_test[test_mask], y_test[test_mask]

        model = xgb.XGBClassifier(**params, eval_metric='auc', random_state=42,
                                   verbosity=0, use_label_encoder=False)
        model.fit(X_train, y_train, eval_set=[(X_test, y_test)], verbose=False)

        y_proba = model.predict_proba(X_test)[:, 1]
        y_pred = (y_proba > 0.31).astype(int)

        try:
            auc = roc_auc_score(y_test, y_proba)
        except ValueError:
            auc = 0.5
        acc = accuracy_score(y_test, y_pred)
        aucs.append(auc)

        fold_start = df.loc[test_idx[0], 'datetime'].strftime('%Y-%m')
        fold_end = df.loc[test_idx[-1], 'datetime'].strftime('%Y-%m')
        period = f"{fold_start} -> {fold_end}"
        verdict = "GOOD" if auc >= 0.75 else ("OK" if auc >= 0.70 else "WEAK")

        print(f"  {i+1:>4} {period:>20} {len(X_train):>7,} {len(X_test):>6,} {auc:>7.4f} {acc:>7.3f} {verdict:>10}")

    print()
    print(f"  Mean AUC: {np.mean(aucs):.4f} +/- {np.std(aucs):.4f}")
    print(f"  Min AUC:  {np.min(aucs):.4f} | Max AUC: {np.max(aucs):.4f}")
    print(f"  Stability: {'STABLE' if np.std(aucs) < 0.05 else 'UNSTABLE'} (std={np.std(aucs):.4f})")


def shap_summary():
    """Show SHAP importance of all 41 features, highlight new ones."""
    print()
    print("=" * 70)
    print("SHAP FEATURE IMPORTANCE (41 features, new ones marked *)")
    print("=" * 70)

    with open(os.path.join(MODELS_DIR, "shap_importance.json")) as f:
        shap = json.load(f)

    new_features = {'momentum_M15', 'volume_spike_M5', 'consecutive_candles_M15',
                    'price_vs_vwap_intraday', 'rsi_H4', 'price_change_H4', 'dist_ema21_H4'}

    print(f"\n  {'#':>3} {'Feature':<28} {'SHAP %':>8} {'Selected':>10}")
    print("  " + "-" * 55)
    for i, (feat, pct) in enumerate(shap.items(), 1):
        marker = " *" if feat in new_features else "  "
        sel = "YES" if pct >= 1.0 else "no"
        print(f"  {i:>3} {feat:<28}{marker} {pct:>7.2f}% {sel:>10}")

    selected_new = [f for f in new_features if shap.get(f, 0) >= 1.0]
    dropped_new = [f for f in new_features if shap.get(f, 0) < 1.0]
    print(f"\n  New features SELECTED ({len(selected_new)}): {selected_new}")
    print(f"  New features DROPPED  ({len(dropped_new)}): {dropped_new}")


def real_examples_this_week():
    """Run v2 model on this week's data with rank-based calibration."""
    print()
    print("=" * 70)
    print("THIS WEEK — v2 Model + Rank-Based Calibration")
    print("=" * 70)

    import MetaTrader5 as mt5
    import xgboost as xgb
    import pandas_ta as ta
    from datetime import datetime, timedelta

    mt5.initialize()

    # H1 data
    rates = mt5.copy_rates_from_pos("XAUUSD", mt5.TIMEFRAME_H1, 0, 500)
    df = pd.DataFrame(rates)
    df['datetime'] = pd.to_datetime(df['time'], unit='s')
    df = df.rename(columns={'tick_volume': 'volume'})

    # H4 data
    h4_rates = mt5.copy_rates_from_pos("XAUUSD", mt5.TIMEFRAME_H4, 0, 200)
    h4 = pd.DataFrame(h4_rates)
    h4['datetime'] = pd.to_datetime(h4['time'], unit='s')

    # M5 data (last ~2 weeks)
    m5_rates = mt5.copy_rates_from_pos("XAUUSD", mt5.TIMEFRAME_M5, 0, 5000)
    m5 = pd.DataFrame(m5_rates)
    m5['datetime'] = pd.to_datetime(m5['time'], unit='s')
    m5 = m5.rename(columns={'tick_volume': 'volume'})

    # XAG
    xag_rates = mt5.copy_rates_from_pos("XAGUSD", mt5.TIMEFRAME_H1, 0, 500)
    xag_df = pd.DataFrame(xag_rates)
    xag_df['datetime'] = pd.to_datetime(xag_df['time'], unit='s')

    mt5.shutdown()

    # === H1 indicators ===
    df['ema_9'] = df['close'].ewm(span=9, adjust=False).mean()
    df['ema_50'] = df['close'].ewm(span=50, adjust=False).mean()
    ema12 = df['close'].ewm(span=12, adjust=False).mean()
    ema26 = df['close'].ewm(span=26, adjust=False).mean()
    df['macd'] = ema12 - ema26
    df['macd_signal'] = df['macd'].ewm(span=9, adjust=False).mean()
    df['macd_hist'] = df['macd'] - df['macd_signal']
    df['rsi_14'] = ta.rsi(df['close'], length=14)

    # === H1 features ===
    df['hour'] = df['datetime'].dt.hour
    df['price_change_1h'] = df['close'].pct_change(1) * 100
    df['price_change_4h'] = df['close'].pct_change(4) * 100
    df['volatility_4h'] = (df['high'].rolling(4).max() - df['low'].rolling(4).min()) / df['close'] * 100
    df['volatility_24h'] = (df['high'].rolling(24).max() - df['low'].rolling(24).min()) / df['close'] * 100
    df['dist_ema9'] = (df['close'] - df['ema_9']) / df['close'] * 100
    df['dist_ema50'] = (df['close'] - df['ema_50']) / df['close'] * 100
    df['macd_momentum'] = df['macd_hist'].diff()
    df['rsi_momentum'] = df['rsi_14'].diff()
    df['is_ny_open'] = df['hour'].apply(lambda h: 1 if h in (13, 14) else 0)
    df['price_change_1W'] = df['close'].pct_change(120) * 100
    df['gold_return_lag1'] = df['close'].pct_change(1).shift(1) * 100

    # XAG
    xag_merged = pd.merge(df[['datetime']], xag_df[['datetime', 'close']].rename(columns={'close': 'xag_close'}),
                          on='datetime', how='left')
    df['xag_change_1h'] = xag_merged['xag_close'].pct_change(1) * 100
    df['xag_change_1h'] = df['xag_change_1h'].fillna(0)

    # Macro (neutral defaults)
    df['dxy_change_1d'] = 0.0
    df['vix_level'] = 17.0
    df['yields_10y_change'] = 0.0

    # === H4 features ===
    h4['rsi_H4'] = ta.rsi(h4['close'], length=14)
    h4['price_change_H4'] = h4['close'].pct_change(1) * 100
    h4['ema21_h4'] = h4['close'].ewm(span=21, adjust=False).mean()
    h4['dist_ema21_H4'] = (h4['close'] - h4['ema21_h4']) / h4['close'] * 100
    h4_feat = h4[['datetime', 'rsi_H4', 'price_change_H4', 'dist_ema21_H4']].sort_values('datetime')
    df = df.sort_values('datetime')
    df = pd.merge_asof(df, h4_feat, on='datetime', direction='backward')

    # === M5 features ===
    vol_sum_3 = m5['volume'].rolling(3).sum()
    vol_avg_20 = m5['volume'].rolling(20).mean()
    m5['volume_spike_M5'] = vol_sum_3 / (3 * vol_avg_20.replace(0, np.nan))
    m5_feat = m5[['datetime', 'volume_spike_M5']].sort_values('datetime')
    df = pd.merge_asof(df, m5_feat, on='datetime', direction='backward')

    # Load model + config
    with open(os.path.join(MODELS_DIR, "xgboost_features.json")) as f:
        feature_cols = json.load(f)
    with open(os.path.join(MODELS_DIR, "xgboost_config.json")) as f:
        cfg = json.load(f)

    model = xgb.XGBClassifier()
    model.load_model(os.path.join(MODELS_DIR, "xgboost_model.json"))

    pcts = cfg.get('probability_percentiles', {})
    p10 = pcts.get('p10', 0.063)
    p50 = pcts.get('p50', 0.234)
    p90 = pcts.get('p90', 0.564)

    # Fill NaN
    for col in feature_cols:
        if col not in df.columns:
            df[col] = 0.0
        df[col] = df[col].fillna(0)

    # Predict
    valid_mask = df[feature_cols].notna().all(axis=1)
    X_all = df.loc[valid_mask, feature_cols].values
    probas = model.predict_proba(X_all)[:, 1]

    # Rank-based calibration
    scores = np.array([rank_calibrate(p, p10, p50, p90) for p in probas])

    df_valid = df.loc[valid_mask].copy()
    df_valid['ml_score'] = scores
    df_valid['raw_proba'] = probas
    df_valid['next_return_pct'] = (df_valid['close'].shift(-1) / df_valid['close'] - 1) * 100

    # Last 7 days
    cutoff = df_valid['datetime'].max() - timedelta(days=7)
    recent = df_valid[df_valid['datetime'] >= cutoff].copy()

    print(f"\nPeriod: {recent['datetime'].iloc[0]} -> {recent['datetime'].iloc[-1]}")
    print(f"Bars: {len(recent)}")
    print(f"Calibration: P10={p10:.4f}->25, P50={p50:.4f}->50, P90={p90:.4f}->75")

    # Score distribution
    print(f"\nSCORE DISTRIBUTION (rank-based, last 7 days):")
    for pct_val in [5, 10, 25, 50, 75, 90, 95]:
        print(f"  P{pct_val}: {np.percentile(recent['ml_score'], pct_val):.1f}")
    print(f"  Mean: {recent['ml_score'].mean():.1f} | Std: {recent['ml_score'].std():.1f}")
    print(f"  Min: {recent['ml_score'].min():.1f} | Max: {recent['ml_score'].max():.1f}")

    # Strong moves
    up_moves = recent[recent['next_return_pct'] > 0.15].sort_values('next_return_pct', ascending=False)
    down_moves = recent[recent['next_return_pct'] < -0.15].sort_values('next_return_pct')
    neutral = recent[(recent['next_return_pct'] >= -0.05) & (recent['next_return_pct'] <= 0.05)]

    print(f"\n{'='*70}")
    print(f"STRONG UP (>{'+'}0.15%): {len(up_moves)} bars | Avg score: {up_moves['ml_score'].mean():.1f}")
    print(f"{'='*70}")
    for _, row in up_moves.head(15).iterrows():
        dt = row['datetime'].strftime('%m-%d %H:%M')
        print(f"  {dt} | {row['close']:.0f} | {row['next_return_pct']:+.3f}% | Score: {row['ml_score']:.1f} | P: {row['raw_proba']:.3f}")

    print(f"\n{'='*70}")
    print(f"STRONG DOWN (<-0.15%): {len(down_moves)} bars | Avg score: {down_moves['ml_score'].mean():.1f}")
    print(f"{'='*70}")
    for _, row in down_moves.head(15).iterrows():
        dt = row['datetime'].strftime('%m-%d %H:%M')
        print(f"  {dt} | {row['close']:.0f} | {row['next_return_pct']:+.3f}% | Score: {row['ml_score']:.1f} | P: {row['raw_proba']:.3f}")

    print(f"\nNEUTRAL: {len(neutral)} bars | Avg score: {neutral['ml_score'].mean():.1f}")

    # Summary
    print(f"\n{'='*70}")
    print("SUMMARY: v2 Model Differentiation")
    print(f"{'='*70}")
    up_avg = up_moves['ml_score'].mean() if len(up_moves) > 0 else 50
    down_avg = down_moves['ml_score'].mean() if len(down_moves) > 0 else 50
    neutral_avg = neutral['ml_score'].mean() if len(neutral) > 0 else 50
    spread = up_avg - down_avg

    print(f"  Strong UP avg:   {up_avg:.1f}")
    print(f"  Neutral avg:     {neutral_avg:.1f}")
    print(f"  Strong DOWN avg: {down_avg:.1f}")
    print(f"  Spread:          {spread:.1f} points")
    print(f"  At 25% weight:   {spread * 0.25:.1f} points on final score")

    if spread > 8:
        print(f"  -> EXCELLENT differentiation")
    elif spread > 5:
        print(f"  -> GOOD differentiation")
    elif spread > 2:
        print(f"  -> MARGINAL differentiation")
    else:
        print(f"  -> POOR differentiation")

    # Compare with v1 (old model had 2.3 point spread)
    print(f"\n  vs v1 model: spread was 2.3 points -> now {spread:.1f} points")


if __name__ == "__main__":
    shap_summary()
    per_fold_auc()
    real_examples_this_week()
