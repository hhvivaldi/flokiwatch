"""
Ensemble Training — XAUUSD Trading Bot (ML v3)
==============================================
Pipeline:
1. Load dataset with 46 features
2. Create labels H1 (>0.15% next bar) and H4 (>0.30% next 4 bars)
3. Walk-forward validation for XGBoost, LightGBM, CatBoost
4. Optuna tuning (50 trials per model)
5. SHAP feature selection
6. Train final models (6 total: 3 algos × 2 horizons)
7. Rank-based calibration percentiles
8. Save ensemble config + all models
"""

import os
import sys
import json
import warnings
import numpy as np
import pandas as pd
from datetime import datetime
from typing import Dict, List, Tuple, Optional

import xgboost as xgb
import lightgbm as lgb
import catboost as cb
from sklearn.metrics import (
    accuracy_score, precision_score, recall_score, f1_score,
    roc_auc_score
)
import optuna
optuna.logging.set_verbosity(optuna.logging.WARNING)

warnings.filterwarnings('ignore')

# Paths
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")
MODELS_DIR = os.path.join(BASE_DIR, "models")
os.makedirs(MODELS_DIR, exist_ok=True)

DATASET_FILE = os.path.join(DATA_DIR, "training_dataset.csv")

# ============================================================================
# FEATURE COLUMNS (46)
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


# ============================================================================
# PART 1: LOAD DATA & CREATE LABELS
# ============================================================================

def load_dataset() -> pd.DataFrame:
    df = pd.read_csv(DATASET_FILE, parse_dates=['datetime'])
    df = df.sort_values('datetime').reset_index(drop=True)
    print(f"✅ Dataset loaded: {len(df):,} rows, {df['datetime'].iloc[0]} → {df['datetime'].iloc[-1]}")
    return df


def create_labels_h1(df: pd.DataFrame, threshold_pct: float = 0.15) -> pd.Series:
    """H1 label: price rises >threshold% in next 1 bar."""
    future_return = df['close'].shift(-1) / df['close'] - 1
    labels = (future_return > threshold_pct / 100).astype(float)
    labels.iloc[-1] = np.nan
    return labels


def create_labels_h4_max(df: pd.DataFrame, threshold_pct: float = 0.30) -> pd.Series:
    """H4 label variant A: price rises >threshold% at ANY point in next 4 bars."""
    future_max = df['close'].rolling(4, min_periods=1).max().shift(-4)
    future_return = future_max / df['close'] - 1
    labels = (future_return > threshold_pct / 100).astype(float)
    labels.iloc[-4:] = np.nan
    return labels


def create_labels_h4_close(df: pd.DataFrame, threshold_pct: float = 0.30) -> pd.Series:
    """H4 label variant B: price rises >threshold% at bar+4 close."""
    future_return = df['close'].shift(-4) / df['close'] - 1
    labels = (future_return > threshold_pct / 100).astype(float)
    labels.iloc[-4:] = np.nan
    return labels


# ============================================================================
# PART 2: WALK-FORWARD VALIDATION
# ============================================================================

def walk_forward_split(df: pd.DataFrame, train_months: int = 12,
                       test_months: int = 2) -> List[Tuple[np.ndarray, np.ndarray]]:
    df_dt = df['datetime']
    min_date = df_dt.min()
    max_date = df_dt.max()

    folds = []
    train_start = min_date

    while True:
        train_end = train_start + pd.DateOffset(months=train_months)
        test_end = train_end + pd.DateOffset(months=test_months)

        if test_end > max_date:
            test_end = max_date
            if train_end >= max_date:
                break

        train_idx = df.index[(df_dt >= train_start) & (df_dt < train_end)].values
        test_idx = df.index[(df_dt >= train_end) & (df_dt < test_end)].values

        if len(train_idx) > 100 and len(test_idx) > 50:
            folds.append((train_idx, test_idx))

        train_start = train_start + pd.DateOffset(months=test_months)

        if train_start + pd.DateOffset(months=train_months) > max_date:
            break

    return folds


def evaluate_fold(y_true, y_pred, y_proba) -> Dict:
    acc = accuracy_score(y_true, y_pred)
    prec = precision_score(y_true, y_pred, zero_division=0)
    rec = recall_score(y_true, y_pred, zero_division=0)
    f1 = f1_score(y_true, y_pred, zero_division=0)
    try:
        auc = roc_auc_score(y_true, y_proba)
    except ValueError:
        auc = 0.5
    return {'accuracy': acc, 'precision': prec, 'recall': rec, 'f1': f1, 'auc': auc}


# ============================================================================
# PART 3: MODEL WRAPPERS
# ============================================================================

def make_model(algo: str, params: Dict):
    """Create a model instance for the given algorithm."""
    if algo == 'xgb':
        return xgb.XGBClassifier(
            **params, eval_metric='auc', random_state=42,
            verbosity=0, use_label_encoder=False, tree_method='hist',
        )
    elif algo == 'lgb':
        return lgb.LGBMClassifier(
            **params, random_state=42, verbosity=-1, n_jobs=-1,
        )
    elif algo == 'cat':
        return cb.CatBoostClassifier(
            **params, random_seed=42, verbose=0, eval_metric='AUC',
        )
    else:
        raise ValueError(f"Unknown algo: {algo}")


def fit_model(model, algo: str, X_train, y_train, X_test, y_test):
    """Fit model with appropriate eval set syntax."""
    if algo == 'xgb':
        model.fit(X_train, y_train, eval_set=[(X_test, y_test)], verbose=False)
    elif algo == 'lgb':
        model.fit(X_train, y_train, eval_set=[(X_test, y_test)])
    elif algo == 'cat':
        model.fit(X_train, y_train, eval_set=(X_test, y_test), early_stopping_rounds=50)
    return model


def predict_proba(model, X):
    """Get probability of class 1."""
    return model.predict_proba(X)[:, 1]


# ============================================================================
# PART 4: WALK-FORWARD EVALUATE
# ============================================================================

def walk_forward_evaluate(df, features, labels, algo, params, verbose=True) -> Dict:
    folds = walk_forward_split(df)
    if verbose:
        print(f"  Walk-forward: {len(folds)} folds ({algo.upper()})")

    all_metrics = []
    all_y_true = []
    all_y_proba = []

    for i, (train_idx, test_idx) in enumerate(folds):
        X_train = df.loc[train_idx, features].values
        y_train = labels.iloc[train_idx].values
        X_test = df.loc[test_idx, features].values
        y_test = labels.iloc[test_idx].values

        train_mask = ~np.isnan(y_train)
        test_mask = ~np.isnan(y_test)
        X_train, y_train = X_train[train_mask], y_train[train_mask]
        X_test, y_test = X_test[test_mask], y_test[test_mask]

        if len(X_test) == 0:
            continue

        model = make_model(algo, params)
        fit_model(model, algo, X_train, y_train, X_test, y_test)

        y_proba = predict_proba(model, X_test)
        y_pred = (y_proba > 0.5).astype(int)

        metrics = evaluate_fold(y_test, y_pred, y_proba)
        all_metrics.append(metrics)
        all_y_true.extend(y_test)
        all_y_proba.extend(y_proba)

        if verbose:
            fold_start = df.loc[test_idx[0], 'datetime'].strftime('%Y-%m')
            fold_end = df.loc[test_idx[-1], 'datetime'].strftime('%Y-%m')
            print(f"    Fold {i+1}: {fold_start}→{fold_end} | "
                  f"AUC={metrics['auc']:.4f} Acc={metrics['accuracy']:.3f} "
                  f"(train={len(X_train):,}, test={len(X_test):,})")

    if not all_metrics:
        return {'auc': 0.5, 'accuracy': 0.5, 'auc_overall': 0.5}

    avg = {}
    for key in all_metrics[0]:
        values = [m[key] for m in all_metrics]
        avg[key] = np.mean(values)
        avg[f'{key}_std'] = np.std(values)

    try:
        avg['auc_overall'] = roc_auc_score(all_y_true, all_y_proba)
    except ValueError:
        avg['auc_overall'] = 0.5

    return avg


# ============================================================================
# PART 5: OPTUNA TUNING
# ============================================================================

def optuna_params(trial, algo: str) -> Dict:
    """Generate hyperparameter search space per algorithm."""
    if algo == 'xgb':
        return {
            'n_estimators': trial.suggest_int('n_estimators', 100, 500),
            'max_depth': trial.suggest_int('max_depth', 3, 8),
            'learning_rate': trial.suggest_float('learning_rate', 0.01, 0.1, log=True),
            'subsample': trial.suggest_float('subsample', 0.7, 0.95),
            'colsample_bytree': trial.suggest_float('colsample_bytree', 0.7, 0.95),
            'min_child_weight': trial.suggest_int('min_child_weight', 1, 10),
            'reg_alpha': trial.suggest_float('reg_alpha', 1e-3, 1.0, log=True),
            'reg_lambda': trial.suggest_float('reg_lambda', 1e-3, 10.0, log=True),
        }
    elif algo == 'lgb':
        return {
            'n_estimators': trial.suggest_int('n_estimators', 100, 500),
            'max_depth': trial.suggest_int('max_depth', 3, 8),
            'learning_rate': trial.suggest_float('learning_rate', 0.01, 0.1, log=True),
            'subsample': trial.suggest_float('subsample', 0.7, 0.95),
            'colsample_bytree': trial.suggest_float('colsample_bytree', 0.7, 0.95),
            'min_child_samples': trial.suggest_int('min_child_samples', 5, 50),
            'reg_alpha': trial.suggest_float('reg_alpha', 1e-3, 1.0, log=True),
            'reg_lambda': trial.suggest_float('reg_lambda', 1e-3, 10.0, log=True),
            'num_leaves': trial.suggest_int('num_leaves', 15, 63),
        }
    elif algo == 'cat':
        return {
            'iterations': trial.suggest_int('iterations', 100, 500),
            'depth': trial.suggest_int('depth', 3, 8),
            'learning_rate': trial.suggest_float('learning_rate', 0.01, 0.1, log=True),
            'l2_leaf_reg': trial.suggest_float('l2_leaf_reg', 1e-3, 10.0, log=True),
            'subsample': trial.suggest_float('subsample', 0.7, 0.95),
            'min_data_in_leaf': trial.suggest_int('min_data_in_leaf', 5, 50),
        }


def run_optuna(df, features, labels, algo, n_trials=50) -> Dict:
    print(f"\n  ⚡ Optuna {algo.upper()} ({n_trials} trials)...")

    def objective(trial):
        params = optuna_params(trial, algo)
        metrics = walk_forward_evaluate(df, features, labels, algo, params, verbose=False)
        return metrics['auc_overall']

    study = optuna.create_study(direction='maximize')
    study.optimize(objective, n_trials=n_trials, show_progress_bar=True)

    print(f"  ✅ Best AUC: {study.best_value:.4f}")
    return study.best_params


# ============================================================================
# PART 6: SHAP ANALYSIS
# ============================================================================

# Features that must always be kept regardless of SHAP importance
FORCE_KEEP_FEATURES = ['price_change_M30']


def run_shap(model, algo, df, features, force_keep=None) -> List[str]:
    """SHAP analysis — returns features with >= 1% importance + force_keep."""
    if force_keep is None:
        force_keep = []
    try:
        import shap
        sample_size = min(2000, len(df))
        sample_idx = np.random.RandomState(42).choice(len(df), sample_size, replace=False)
        X_sample = df.iloc[sample_idx][features].values

        if algo == 'cat':
            explainer = shap.TreeExplainer(model)
        else:
            explainer = shap.TreeExplainer(model)

        shap_values = explainer.shap_values(X_sample)
        if isinstance(shap_values, list):
            shap_values = shap_values[1]  # class 1 for binary

        mean_abs = np.abs(shap_values).mean(axis=0)
        total = mean_abs.sum()

        importance = pd.DataFrame({
            'feature': features,
            'pct': (mean_abs / total * 100) if total > 0 else 0,
        }).sort_values('pct', ascending=False)

        selected = importance[importance['pct'] >= 1.0]['feature'].tolist()

        # Force-keep features even if SHAP < 1%
        forced_added = []
        for fk in force_keep:
            if fk in features and fk not in selected:
                selected.append(fk)
                forced_added.append(fk)

        print(f"    SHAP: {len(selected)}/{len(features)} features >= 1%" +
              (f" (+{len(forced_added)} forced)" if forced_added else ""))
        for _, row in importance.head(10).iterrows():
            marker = "✅" if row['pct'] >= 1.0 else "❌"
            print(f"      {marker} {row['feature']:<28} {row['pct']:>6.2f}%")

        # Log forced features SHAP values
        for fk in force_keep:
            if fk in features:
                fk_row = importance[importance['feature'] == fk]
                if not fk_row.empty:
                    pct_val = fk_row.iloc[0]['pct']
                    if pct_val >= 1.0:
                        status = "✅ >= 1%"
                    elif pct_val < 0.1:
                        status = "⚠️ < 0.1% (RECONSIDER)"
                    else:
                        status = "ℹ️ < 1% (forced)"
                    print(f"    📌 FORCED: {fk:<28} {pct_val:>6.2f}% — {status}")

        return selected, importance

    except Exception as e:
        print(f"    ⚠️ SHAP failed: {e} — keeping all features")
        return features, None


# ============================================================================
# PART 7: TRAIN FINAL MODEL
# ============================================================================

def train_final(df, features, labels, algo, params) -> Tuple:
    """Train final model on all data except last 2 months."""
    df_dt = df['datetime']
    cutoff = df_dt.max() - pd.DateOffset(months=2)
    train_idx = df.index[df_dt < cutoff].values
    test_idx = df.index[df_dt >= cutoff].values

    X_train = df.loc[train_idx, features].values
    y_train = labels.iloc[train_idx].values
    X_test = df.loc[test_idx, features].values
    y_test = labels.iloc[test_idx].values

    train_mask = ~np.isnan(y_train)
    test_mask = ~np.isnan(y_test)
    X_train, y_train = X_train[train_mask], y_train[train_mask]
    X_test, y_test = X_test[test_mask], y_test[test_mask]

    model = make_model(algo, params)
    fit_model(model, algo, X_train, y_train, X_test, y_test)

    y_proba = predict_proba(model, X_test)

    # Optimal threshold
    best_thresh, best_f1 = 0.5, 0
    for t in np.arange(0.25, 0.75, 0.01):
        f1_t = f1_score(y_test, (y_proba > t).astype(int), zero_division=0)
        if f1_t > best_f1:
            best_f1 = f1_t
            best_thresh = t

    y_pred = (y_proba > best_thresh).astype(int)
    metrics = evaluate_fold(y_test, y_pred, y_proba)
    metrics['optimal_threshold'] = float(best_thresh)

    # Probability percentiles for rank-based calibration
    percentiles = {}
    for pct in [5, 10, 25, 50, 75, 90, 95]:
        percentiles[f'p{pct}'] = float(np.percentile(y_proba, pct))
    metrics['probability_percentiles'] = percentiles

    print(f"    Holdout: AUC={metrics['auc']:.4f} Acc={metrics['accuracy']:.4f} "
          f"F1={metrics['f1']:.4f} Thresh={best_thresh:.2f}")
    print(f"    Percentiles: P10={percentiles['p10']:.4f} P50={percentiles['p50']:.4f} P90={percentiles['p90']:.4f}")

    return model, metrics


# ============================================================================
# PART 8: SAVE MODELS
# ============================================================================

def save_model_file(model, algo, horizon):
    """Save individual model file."""
    if algo == 'xgb':
        path = os.path.join(MODELS_DIR, f"ensemble_{algo}_{horizon}.json")
        model.save_model(path)
    elif algo == 'lgb':
        path = os.path.join(MODELS_DIR, f"ensemble_{algo}_{horizon}.txt")
        model.booster_.save_model(path)
    elif algo == 'cat':
        path = os.path.join(MODELS_DIR, f"ensemble_{algo}_{horizon}.cbm")
        model.save_model(path)
    print(f"    💾 Saved: {os.path.basename(path)}")
    return path


# ============================================================================
# MAIN PIPELINE
# ============================================================================

def train_one_pipeline(df, features_all, labels, algo, horizon, n_trials=50):
    """Full pipeline for one algo + one horizon."""
    print(f"\n{'='*70}")
    print(f"🏋️ {algo.upper()} — {horizon.upper()} HORIZON")
    print(f"{'='*70}")

    n_pos = labels.dropna().sum()
    n_total = labels.dropna().count()
    print(f"  Labels: {int(n_pos):,} positive ({n_pos/n_total*100:.1f}%) / "
          f"{int(n_total-n_pos):,} negative ({(n_total-n_pos)/n_total*100:.1f}%)")

    # Optuna
    best_params = run_optuna(df, features_all, labels, algo, n_trials)

    # Walk-forward with best params
    print(f"\n  📊 Walk-forward with best params:")
    wf_metrics = walk_forward_evaluate(df, features_all, labels, algo, best_params, verbose=True)
    print(f"  → Overall AUC: {wf_metrics['auc_overall']:.4f} (avg: {wf_metrics['auc']:.4f} ± {wf_metrics['auc_std']:.4f})")

    # Train full model for SHAP
    print(f"\n  🔍 SHAP analysis (all {len(features_all)} features):")
    model_full, _ = train_final(df, features_all, labels, algo, best_params)
    selected, shap_df = run_shap(model_full, algo, df, features_all, force_keep=FORCE_KEEP_FEATURES)

    # Retrain with selected features if different
    final_features = features_all
    if len(selected) < len(features_all):
        wf_sel = walk_forward_evaluate(df, selected, labels, algo, best_params, verbose=False)
        if wf_sel['auc_overall'] >= wf_metrics['auc_overall'] - 0.005:
            print(f"  ✅ Using {len(selected)} selected features (AUC: {wf_sel['auc_overall']:.4f})")
            final_features = selected
            wf_metrics = wf_sel
        else:
            print(f"  ⚠️ Selected features AUC dropped ({wf_sel['auc_overall']:.4f} vs {wf_metrics['auc_overall']:.4f}). Keeping all.")

    # Final model
    print(f"\n  🏋️ Training final {algo.upper()} model ({len(final_features)} features):")
    model_final, holdout_metrics = train_final(df, final_features, labels, algo, best_params)

    # Save
    model_path = save_model_file(model_final, algo, horizon)

    return {
        'algo': algo,
        'horizon': horizon,
        'features': final_features,
        'params': best_params,
        'holdout_metrics': {k: round(v, 6) for k, v in holdout_metrics.items() if k != 'probability_percentiles'},
        'probability_percentiles': holdout_metrics.get('probability_percentiles', {}),
        'wf_auc_overall': round(wf_metrics['auc_overall'], 6),
        'wf_auc_mean': round(wf_metrics['auc'], 6),
        'wf_auc_std': round(wf_metrics.get('auc_std', 0), 6),
        'model_path': os.path.basename(model_path),
        'shap_importance': {row['feature']: round(row['pct'], 4) for _, row in shap_df.iterrows()} if shap_df is not None else {},
    }


def main():
    print("=" * 70)
    print("🧠 ENSEMBLE TRAINING PIPELINE — ML v3")
    print("=" * 70)
    print(f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Models: XGBoost + LightGBM + CatBoost")
    print(f"Horizons: H1 (>0.15% next bar) + H4 (>0.30% next 4 bars)")
    print()

    # --- Load data ---
    df = load_dataset()

    # --- Create labels ---
    labels_h1 = create_labels_h1(df, 0.15)
    labels_h4_max = create_labels_h4_max(df, 0.30)
    labels_h4_close = create_labels_h4_close(df, 0.30)

    n_h1 = labels_h1.dropna().sum()
    n_h4m = labels_h4_max.dropna().sum()
    n_h4c = labels_h4_close.dropna().sum()
    n_total = labels_h1.dropna().count()
    print(f"\nLabel distributions:")
    print(f"  H1 (>0.15% next bar):     {int(n_h1):,} positive ({n_h1/n_total*100:.1f}%)")
    print(f"  H4-max (>0.30% max 4bar): {int(n_h4m):,} positive ({n_h4m/labels_h4_max.dropna().count()*100:.1f}%)")
    print(f"  H4-close (>0.30% bar+4):  {int(n_h4c):,} positive ({n_h4c/labels_h4_close.dropna().count()*100:.1f}%)")

    # --- Compare H4 label variants with quick XGB ---
    print(f"\n{'='*70}")
    print("📊 H4 LABEL COMPARISON (XGBoost, default params)")
    print(f"{'='*70}")
    default_xgb = {
        'n_estimators': 200, 'max_depth': 5, 'learning_rate': 0.05,
        'subsample': 0.8, 'colsample_bytree': 0.8, 'min_child_weight': 5,
        'reg_alpha': 0.1, 'reg_lambda': 1.0,
    }
    wf_h4_max = walk_forward_evaluate(df, FEATURE_COLUMNS, labels_h4_max, 'xgb', default_xgb, verbose=False)
    wf_h4_close = walk_forward_evaluate(df, FEATURE_COLUMNS, labels_h4_close, 'xgb', default_xgb, verbose=False)
    print(f"  H4-max AUC:   {wf_h4_max['auc_overall']:.4f} (avg: {wf_h4_max['auc']:.4f})")
    print(f"  H4-close AUC: {wf_h4_close['auc_overall']:.4f} (avg: {wf_h4_close['auc']:.4f})")

    # Pick best H4 label
    if wf_h4_max['auc_overall'] > wf_h4_close['auc_overall'] + 0.02:
        print(f"  ⚠️ H4-max much better — possible overfitting to peak. Using H4-close for safety.")
        labels_h4 = labels_h4_close
        h4_label_type = 'close'
    elif wf_h4_close['auc_overall'] > wf_h4_max['auc_overall']:
        print(f"  ✅ H4-close better or equal. Using H4-close.")
        labels_h4 = labels_h4_close
        h4_label_type = 'close'
    else:
        print(f"  ✅ H4-max slightly better (within 0.02). Using H4-max.")
        labels_h4 = labels_h4_max
        h4_label_type = 'max'

    # --- Train all 6 models ---
    ALGOS = ['xgb', 'lgb', 'cat']
    N_TRIALS = 50
    results = {}

    for algo in ALGOS:
        # H1
        result_h1 = train_one_pipeline(df, FEATURE_COLUMNS, labels_h1, algo, 'h1', N_TRIALS)
        results[f'{algo}_h1'] = result_h1

        # H4
        result_h4 = train_one_pipeline(df, FEATURE_COLUMNS, labels_h4, algo, 'h4', N_TRIALS)
        results[f'{algo}_h4'] = result_h4

    # --- Compute ensemble weights (AUC-proportional per horizon) ---
    print(f"\n{'='*70}")
    print("⚖️ ENSEMBLE WEIGHTS (AUC-proportional)")
    print(f"{'='*70}")

    weights = {}
    for horizon in ['h1', 'h4']:
        aucs = {algo: results[f'{algo}_{horizon}']['wf_auc_overall'] for algo in ALGOS}
        total_auc = sum(aucs.values())
        w = {algo: round(aucs[algo] / total_auc, 4) for algo in ALGOS}
        weights[horizon] = w
        print(f"  {horizon.upper()}: " + " | ".join(f"{a.upper()}={w[a]:.3f} (AUC={aucs[a]:.4f})" for a in ALGOS))

    # --- Save ensemble config ---
    print(f"\n{'='*70}")
    print("💾 SAVING ENSEMBLE CONFIG")
    print(f"{'='*70}")

    # Features per horizon (union of selected features across algos)
    features_h1 = list(set().union(*[results[f'{a}_h1']['features'] for a in ALGOS]))
    features_h4 = list(set().union(*[results[f'{a}_h4']['features'] for a in ALGOS]))

    # Save feature lists
    with open(os.path.join(MODELS_DIR, "ensemble_features_h1.json"), 'w') as f:
        json.dump(features_h1, f, indent=2)
    with open(os.path.join(MODELS_DIR, "ensemble_features_h4.json"), 'w') as f:
        json.dump(features_h4, f, indent=2)

    # Save SHAP importance (combined)
    all_shap = {}
    for key, res in results.items():
        all_shap[key] = res.get('shap_importance', {})
    with open(os.path.join(MODELS_DIR, "ensemble_shap.json"), 'w') as f:
        json.dump(all_shap, f, indent=2)

    # Save main config
    config = {
        'model_type': 'ensemble',
        'algos': ALGOS,
        'horizons': ['h1', 'h4'],
        'horizon_blend': {'h1': 0.4, 'h4': 0.6},
        'ensemble_weights': weights,
        'h4_label_type': h4_label_type,
        'h4_label_comparison': {
            'max_auc': round(wf_h4_max['auc_overall'], 6),
            'close_auc': round(wf_h4_close['auc_overall'], 6),
        },
        'models': {},
        'trained_at': datetime.now().isoformat(),
    }

    for key, res in results.items():
        config['models'][key] = {
            'model_file': res['model_path'],
            'features': res['features'],
            'n_features': len(res['features']),
            'params': res['params'],
            'holdout_metrics': res['holdout_metrics'],
            'probability_percentiles': res['probability_percentiles'],
            'wf_auc_overall': res['wf_auc_overall'],
            'wf_auc_mean': res['wf_auc_mean'],
            'wf_auc_std': res['wf_auc_std'],
        }

    config_path = os.path.join(MODELS_DIR, "ensemble_config.json")
    with open(config_path, 'w') as f:
        json.dump(config, f, indent=2)
    print(f"  ✅ Config: {config_path}")

    # --- Summary ---
    print(f"\n{'='*70}")
    print("📊 FINAL SUMMARY")
    print(f"{'='*70}")

    print(f"\n  H4 label: {h4_label_type} (max AUC={wf_h4_max['auc_overall']:.4f}, close AUC={wf_h4_close['auc_overall']:.4f})")

    print(f"\n  {'Model':<12} {'WF AUC':>10} {'Holdout AUC':>12} {'Features':>10}")
    print(f"  {'─'*48}")
    for key in sorted(results.keys()):
        r = results[key]
        print(f"  {key:<12} {r['wf_auc_overall']:>10.4f} {r['holdout_metrics']['auc']:>12.4f} {len(r['features']):>10}")

    # Ensemble estimate (weighted average of WF AUCs)
    for horizon in ['h1', 'h4']:
        w = weights[horizon]
        ens_auc = sum(w[a] * results[f'{a}_{horizon}']['wf_auc_overall'] for a in ALGOS)
        print(f"\n  Ensemble {horizon.upper()} (weighted): ~{ens_auc:.4f}")

    print(f"\n  vs v2 XGBoost-only: 0.795 (H1 only)")

    print(f"\n  Files saved:")
    for algo in ALGOS:
        for h in ['h1', 'h4']:
            print(f"    models/{results[f'{algo}_{h}']['model_path']}")
    print(f"    models/ensemble_config.json")
    print(f"    models/ensemble_features_h1.json")
    print(f"    models/ensemble_features_h4.json")
    print(f"    models/ensemble_shap.json")

    # --- M5 SHAP Comparison (v3 baseline vs v3.1 new) ---
    print(f"\n{'='*70}")
    print("🔬 M5 FEATURE SHAP COMPARISON (v3 baseline vs v3.1)")
    print(f"{'='*70}")

    M5_TRACK_FEATURES = [
        'momentum_x_volume', 'volume_spike_M5', 'price_change_M30',
        'momentum_M15', 'price_vs_vwap_intraday', 'consecutive_candles_M15',
        'trend_x_session', 'price_change_M15',
    ]

    # v3 baseline SHAP (from models_v3_backup/ensemble_shap.json)
    v3_shap_path = os.path.join(BASE_DIR, "models_v3_backup", "ensemble_shap.json")
    v3_shap = {}
    if os.path.exists(v3_shap_path):
        with open(v3_shap_path) as f:
            v3_shap = json.load(f)
        print(f"  ✅ v3 baseline loaded from {v3_shap_path}")
    else:
        print(f"  ⚠️ v3 baseline not found at {v3_shap_path}")

    print(f"\n  {'Feature':<28} ", end="")
    for key in sorted(results.keys()):
        print(f"{'v3':>6} {'v3.1':>6} ", end="")
    print()
    print(f"  {'─'*28} " + "─"*13*len(results))

    m5_warnings = []
    for feat in M5_TRACK_FEATURES:
        has_data = False
        for key in sorted(results.keys()):
            new_val = all_shap.get(key, {}).get(feat, 0)
            if new_val > 0:
                has_data = True
                break
            old_val = v3_shap.get(key, {}).get(feat, 0)
            if old_val > 0:
                has_data = True
                break
        if not has_data:
            continue

        print(f"  {feat:<28} ", end="")
        for key in sorted(results.keys()):
            old_val = v3_shap.get(key, {}).get(feat, 0)
            new_val = all_shap.get(key, {}).get(feat, 0)
            marker = ""
            if old_val > 1.0 and new_val < old_val * 0.5:
                marker = " ⚠️"
                m5_warnings.append(f"{feat} in {key}: {old_val:.1f}% → {new_val:.1f}%")
            print(f"{old_val:>6.1f} {new_val:>6.1f}{marker} ", end="")
        print()

    if m5_warnings:
        print(f"\n  ⚠️ M5 SHAP WARNINGS (>50% drop from v3):")
        for w in m5_warnings:
            print(f"    - {w}")
    else:
        print(f"\n  ✅ No significant M5 SHAP drops detected")

    # Check momentum_x_volume threshold: must stay ≥5% in at least 4/6 models
    mxv_above_5 = sum(1 for key in results if all_shap.get(key, {}).get('momentum_x_volume', 0) >= 5.0)
    print(f"\n  momentum_x_volume ≥5% SHAP: {mxv_above_5}/6 models", end="")
    if mxv_above_5 >= 4:
        print(" ✅")
    else:
        print(" ⚠️ BELOW THRESHOLD (need ≥4/6)")

    print(f"\n{'='*70}")
    print("✅ ENSEMBLE TRAINING COMPLETE!")
    print(f"{'='*70}")


if __name__ == "__main__":
    main()
