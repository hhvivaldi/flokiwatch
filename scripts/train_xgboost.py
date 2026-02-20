"""
XGBoost Training — XAUUSD Trading Bot
=====================================
Full pipeline:
1. Load dataset with 34 features
2. Test multiple label thresholds (0.05%, 0.10%, 0.15%)
3. Walk-forward validation (6 folds, 12-month train / 2-month test)
4. Optuna hyperparameter tuning (100 trials)
5. SHAP analysis + feature selection
6. Retrain final model with selected features
7. Save model + config for production
"""

import os
import sys
import json
import warnings
import numpy as np
import pandas as pd
from datetime import datetime
from typing import Dict, List, Tuple

import xgboost as xgb
from sklearn.metrics import (
    accuracy_score, precision_score, recall_score, f1_score,
    roc_auc_score, confusion_matrix, classification_report
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
# FEATURE COLUMNS (34)
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
    # Group 7: M5/M15 Microstructure (4)
    'momentum_M15', 'volume_spike_M5', 'consecutive_candles_M15', 'price_vs_vwap_intraday',
    # Group 8: H4 Multi-Timeframe (3)
    'rsi_H4', 'price_change_H4', 'dist_ema21_H4',
]


# ============================================================================
# PART 1: LOAD DATA & CREATE LABELS
# ============================================================================

def load_dataset() -> pd.DataFrame:
    """Load the training dataset."""
    df = pd.read_csv(DATASET_FILE, parse_dates=['datetime'])
    df = df.sort_values('datetime').reset_index(drop=True)
    print(f"✅ Dataset loaded: {len(df):,} rows, {df['datetime'].iloc[0]} → {df['datetime'].iloc[-1]}")
    return df


def create_labels(df: pd.DataFrame, threshold_pct: float) -> pd.Series:
    """
    Create binary labels: 1 if price rose > threshold_pct in next H1 bar.
    """
    future_return = df['close'].shift(-1) / df['close'] - 1
    labels = (future_return > threshold_pct / 100).astype(int)
    # Last row has no future — drop it
    labels.iloc[-1] = np.nan
    return labels


# ============================================================================
# PART 2: WALK-FORWARD VALIDATION
# ============================================================================

def walk_forward_split(df: pd.DataFrame, train_months: int = 12,
                       test_months: int = 2) -> List[Tuple[np.ndarray, np.ndarray]]:
    """
    Generate walk-forward fold indices.
    Train window: train_months rolling, Test window: test_months.
    Step: test_months (no overlap in test sets).
    """
    df_dt = df['datetime']
    min_date = df_dt.min()
    max_date = df_dt.max()

    folds = []
    train_start = min_date

    while True:
        train_end = train_start + pd.DateOffset(months=train_months)
        test_end = train_end + pd.DateOffset(months=test_months)

        if test_end > max_date:
            # Use remaining data as last test set if enough
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


def evaluate_fold(y_true: np.ndarray, y_pred: np.ndarray,
                  y_proba: np.ndarray) -> Dict:
    """Calculate metrics for a single fold."""
    acc = accuracy_score(y_true, y_pred)
    prec = precision_score(y_true, y_pred, zero_division=0)
    rec = recall_score(y_true, y_pred, zero_division=0)
    f1 = f1_score(y_true, y_pred, zero_division=0)
    try:
        auc = roc_auc_score(y_true, y_proba)
    except ValueError:
        auc = 0.5
    return {'accuracy': acc, 'precision': prec, 'recall': rec, 'f1': f1, 'auc': auc}


def walk_forward_evaluate(df: pd.DataFrame, features: List[str],
                          labels: pd.Series, params: Dict,
                          verbose: bool = True) -> Dict:
    """
    Run walk-forward validation with given XGBoost params.
    Returns average metrics across folds.
    """
    folds = walk_forward_split(df)

    if verbose:
        print(f"  Walk-forward: {len(folds)} folds")

    all_metrics = []
    all_y_true = []
    all_y_proba = []

    for i, (train_idx, test_idx) in enumerate(folds):
        X_train = df.loc[train_idx, features].values
        y_train = labels.iloc[train_idx].values
        X_test = df.loc[test_idx, features].values
        y_test = labels.iloc[test_idx].values

        # Remove NaN rows
        train_mask = ~np.isnan(y_train)
        test_mask = ~np.isnan(y_test)
        X_train, y_train = X_train[train_mask], y_train[train_mask]
        X_test, y_test = X_test[test_mask], y_test[test_mask]

        if len(X_test) == 0:
            continue

        # No scale_pos_weight: 75/25 imbalance is moderate, XGBoost handles it.
        # scale_pos_weight distorts probability calibration (critical for 0-100 score).
        model = xgb.XGBClassifier(
            **params,
            eval_metric='auc',
            random_state=42,
            verbosity=0,
            use_label_encoder=False,
        )

        model.fit(
            X_train, y_train,
            eval_set=[(X_test, y_test)],
            verbose=False,
        )

        y_proba = model.predict_proba(X_test)[:, 1]
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
                  f"Prec={metrics['precision']:.3f} Rec={metrics['recall']:.3f} "
                  f"(train={len(X_train):,}, test={len(X_test):,})")

    if not all_metrics:
        return {'auc': 0.5, 'accuracy': 0.5, 'precision': 0, 'recall': 0, 'f1': 0}

    # Average metrics
    avg = {}
    for key in all_metrics[0]:
        values = [m[key] for m in all_metrics]
        avg[key] = np.mean(values)
        avg[f'{key}_std'] = np.std(values)

    # Overall AUC on all test predictions combined
    try:
        avg['auc_overall'] = roc_auc_score(all_y_true, all_y_proba)
    except ValueError:
        avg['auc_overall'] = 0.5

    return avg


# ============================================================================
# PART 3: MULTI-THRESHOLD TESTING
# ============================================================================

def test_thresholds(df: pd.DataFrame, features: List[str]) -> Tuple[float, pd.Series]:
    """
    Test multiple label thresholds and pick the best by AUC.
    """
    print(f"\n{'='*70}")
    print("🎯 MULTI-THRESHOLD TESTING")
    print(f"{'='*70}")

    thresholds = [0.05, 0.10, 0.15]
    default_params = {
        'n_estimators': 200,
        'max_depth': 5,
        'learning_rate': 0.05,
        'subsample': 0.8,
        'colsample_bytree': 0.8,
        'min_child_weight': 5,
        'reg_alpha': 0.1,
        'reg_lambda': 1.0,
    }

    results = {}
    for thresh in thresholds:
        print(f"\n--- Threshold: {thresh}% ---")
        labels = create_labels(df, thresh)
        n_pos = labels.dropna().sum()
        n_total = labels.dropna().count()
        print(f"  Label distribution: {n_pos:,} positives ({n_pos/n_total*100:.1f}%) / "
              f"{n_total - n_pos:,} negatives ({(n_total-n_pos)/n_total*100:.1f}%)")

        metrics = walk_forward_evaluate(df, features, labels, default_params, verbose=True)
        results[thresh] = metrics
        print(f"  → Avg AUC: {metrics['auc']:.4f} ± {metrics['auc_std']:.4f} | "
              f"Overall AUC: {metrics['auc_overall']:.4f} | "
              f"Avg Acc: {metrics['accuracy']:.3f} ± {metrics['accuracy_std']:.3f}")

    # Pick best threshold by overall AUC
    best_thresh = max(results, key=lambda t: results[t]['auc_overall'])
    print(f"\n✅ Best threshold: {best_thresh}% (AUC overall: {results[best_thresh]['auc_overall']:.4f})")

    best_labels = create_labels(df, best_thresh)
    return best_thresh, best_labels


# ============================================================================
# PART 4: OPTUNA HYPERPARAMETER TUNING
# ============================================================================

def optuna_objective(trial, df, features, labels):
    """Optuna objective: maximize walk-forward AUC."""
    params = {
        'n_estimators': trial.suggest_int('n_estimators', 100, 500),
        'max_depth': trial.suggest_int('max_depth', 3, 8),
        'learning_rate': trial.suggest_float('learning_rate', 0.01, 0.1, log=True),
        'subsample': trial.suggest_float('subsample', 0.7, 0.95),
        'colsample_bytree': trial.suggest_float('colsample_bytree', 0.7, 0.95),
        'min_child_weight': trial.suggest_int('min_child_weight', 1, 10),
        'reg_alpha': trial.suggest_float('reg_alpha', 1e-3, 1.0, log=True),
        'reg_lambda': trial.suggest_float('reg_lambda', 1e-3, 10.0, log=True),
    }

    metrics = walk_forward_evaluate(df, features, labels, params, verbose=False)
    return metrics['auc_overall']


def run_optuna_tuning(df: pd.DataFrame, features: List[str],
                      labels: pd.Series, n_trials: int = 100) -> Dict:
    """Run Optuna hyperparameter search."""
    print(f"\n{'='*70}")
    print(f"⚡ OPTUNA HYPERPARAMETER TUNING ({n_trials} trials)")
    print(f"{'='*70}")

    study = optuna.create_study(direction='maximize',
                                 study_name='xgboost_xauusd')

    def objective(trial):
        return optuna_objective(trial, df, features, labels)

    study.optimize(objective, n_trials=n_trials, show_progress_bar=True)

    best_params = study.best_params
    best_auc = study.best_value

    print(f"\n✅ Best AUC (overall): {best_auc:.4f}")
    print(f"   Best params:")
    for k, v in best_params.items():
        print(f"     {k}: {v}")

    return best_params


# ============================================================================
# PART 5: TRAIN FINAL MODEL
# ============================================================================

def train_final_model(df: pd.DataFrame, features: List[str],
                      labels: pd.Series, params: Dict) -> Tuple[xgb.XGBClassifier, Dict]:
    """
    Train the final model on ALL data (except last 2 months held out for sanity).
    Returns model and test metrics.
    """
    print(f"\n{'='*70}")
    print("🏋️ TRAINING FINAL MODEL")
    print(f"{'='*70}")

    # Hold out last 2 months for final sanity check
    df_dt = df['datetime']
    cutoff = df_dt.max() - pd.DateOffset(months=2)
    train_idx = df.index[df_dt < cutoff].values
    test_idx = df.index[df_dt >= cutoff].values

    X_train = df.loc[train_idx, features].values
    y_train = labels.iloc[train_idx].values
    X_test = df.loc[test_idx, features].values
    y_test = labels.iloc[test_idx].values

    # Remove NaN
    train_mask = ~np.isnan(y_train)
    test_mask = ~np.isnan(y_test)
    X_train, y_train = X_train[train_mask], y_train[train_mask]
    X_test, y_test = X_test[test_mask], y_test[test_mask]

    n_pos = y_train.sum()
    print(f"  Train: {len(X_train):,} samples | Test (holdout): {len(X_test):,} samples")
    print(f"  Train positives: {int(n_pos):,} ({n_pos/len(y_train)*100:.1f}%)")

    # No scale_pos_weight: 75/25 imbalance is moderate, XGBoost handles it.
    # scale_pos_weight distorts probability calibration (critical for 0-100 score).
    model = xgb.XGBClassifier(
        **params,
        eval_metric='auc',
        random_state=42,
        verbosity=0,
        use_label_encoder=False,
        tree_method='hist',
    )

    model.fit(
        X_train, y_train,
        eval_set=[(X_test, y_test)],
        verbose=False,
    )

    # Evaluate on holdout
    y_proba = model.predict_proba(X_test)[:, 1]

    # Find optimal prediction threshold (maximize F1)
    best_thresh = 0.5
    best_f1 = 0
    for t in np.arange(0.30, 0.75, 0.01):
        y_t = (y_proba > t).astype(int)
        f1_t = f1_score(y_test, y_t, zero_division=0)
        if f1_t > best_f1:
            best_f1 = f1_t
            best_thresh = t

    print(f"\n  🎯 Optimal prediction threshold: {best_thresh:.2f} (F1={best_f1:.4f})")

    # Metrics at default 0.5
    y_pred_50 = (y_proba > 0.5).astype(int)
    metrics_50 = evaluate_fold(y_test, y_pred_50, y_proba)

    # Metrics at optimal threshold
    y_pred_opt = (y_proba > best_thresh).astype(int)
    metrics_opt = evaluate_fold(y_test, y_pred_opt, y_proba)

    cm_opt = confusion_matrix(y_test, y_pred_opt)

    print(f"\n  📊 Holdout Metrics (threshold={best_thresh:.2f}):")
    print(f"     AUC:       {metrics_opt['auc']:.4f}")
    print(f"     Accuracy:  {metrics_opt['accuracy']:.4f}")
    print(f"     Precision: {metrics_opt['precision']:.4f}")
    print(f"     Recall:    {metrics_opt['recall']:.4f}")
    print(f"     F1:        {metrics_opt['f1']:.4f}")
    print(f"\n  📊 Holdout Metrics (threshold=0.50 for comparison):")
    print(f"     Accuracy:  {metrics_50['accuracy']:.4f}")
    print(f"     Precision: {metrics_50['precision']:.4f}")
    print(f"     Recall:    {metrics_50['recall']:.4f}")
    print(f"\n  Confusion Matrix (threshold={best_thresh:.2f}):")
    print(f"              Predicted")
    print(f"              0     1")
    print(f"  Actual 0  {cm_opt[0,0]:5d} {cm_opt[0,1]:5d}")
    print(f"         1  {cm_opt[1,0]:5d} {cm_opt[1,1]:5d}")

    # Baseline comparison
    baseline = max(y_test.mean(), 1 - y_test.mean())
    print(f"\n  Baseline (always majority): {baseline:.4f}")
    print(f"  Model accuracy:             {metrics_opt['accuracy']:.4f}")
    print(f"  Gain:                       {(metrics_opt['accuracy'] - baseline)*100:+.2f}%")

    # Check for degenerate model
    unique_preds = np.unique(y_pred_opt)
    if len(unique_preds) == 1:
        print(f"\n  ⚠️ WARNING: Model predicts only class {unique_preds[0]}! Degenerate model.")
    else:
        pred_dist = np.bincount(y_pred_opt)
        print(f"\n  Prediction distribution: 0={pred_dist[0]:,} 1={pred_dist[1]:,}")

    # Probability distribution analysis
    print(f"\n  📊 Probability Distribution:")
    for pct in [10, 25, 50, 75, 90]:
        print(f"     P{pct}: {np.percentile(y_proba, pct):.4f}")
    print(f"     Mean: {y_proba.mean():.4f} | Std: {y_proba.std():.4f}")

    metrics_opt['optimal_threshold'] = float(best_thresh)

    # Save probability percentiles for rank-based calibration
    percentiles = {}
    for pct in [5, 10, 25, 50, 75, 90, 95]:
        percentiles[f'p{pct}'] = float(np.percentile(y_proba, pct))
    metrics_opt['probability_percentiles'] = percentiles
    print(f"\n  📊 Probability percentiles (for rank-based calibration):")
    for k, v in percentiles.items():
        print(f"     {k}: {v:.4f}")

    return model, metrics_opt


# ============================================================================
# PART 6: SHAP ANALYSIS
# ============================================================================

def run_shap_analysis(model: xgb.XGBClassifier, df: pd.DataFrame,
                      features: List[str], labels: pd.Series) -> List[str]:
    """
    Run SHAP analysis. Returns list of features to keep (importance >= 1%).
    """
    print(f"\n{'='*70}")
    print("🔍 SHAP ANALYSIS")
    print(f"{'='*70}")

    try:
        import shap

        # Use a sample for SHAP (faster)
        sample_size = min(2000, len(df))
        sample_idx = np.random.RandomState(42).choice(len(df), sample_size, replace=False)
        X_sample = df.iloc[sample_idx][features].values

        explainer = shap.TreeExplainer(model)
        shap_values = explainer.shap_values(X_sample)

        # Mean absolute SHAP value per feature
        mean_abs_shap = np.abs(shap_values).mean(axis=0)
        total_shap = mean_abs_shap.sum()

        # Feature importance table
        importance_df = pd.DataFrame({
            'feature': features,
            'mean_abs_shap': mean_abs_shap,
            'importance_pct': (mean_abs_shap / total_shap * 100) if total_shap > 0 else 0,
        }).sort_values('importance_pct', ascending=False)

        print(f"\n  📊 SHAP Feature Importance (top 34):")
        print(f"  {'Feature':<25} {'SHAP':>10} {'%':>8}")
        print(f"  {'─'*45}")
        for _, row in importance_df.iterrows():
            marker = "✅" if row['importance_pct'] >= 1.0 else "❌"
            print(f"  {marker} {row['feature']:<23} {row['mean_abs_shap']:>10.4f} {row['importance_pct']:>7.2f}%")

        # Feature selection: keep features with >= 1% importance
        selected = importance_df[importance_df['importance_pct'] >= 1.0]['feature'].tolist()
        dropped = importance_df[importance_df['importance_pct'] < 1.0]['feature'].tolist()

        print(f"\n  ✅ Features to keep ({len(selected)}): {selected}")
        if dropped:
            print(f"  ❌ Features to drop ({len(dropped)}): {dropped}")

        # Save SHAP importance to file
        importance_file = os.path.join(MODELS_DIR, "shap_importance.json")
        importance_dict = {row['feature']: round(row['importance_pct'], 4)
                          for _, row in importance_df.iterrows()}
        with open(importance_file, 'w') as f:
            json.dump(importance_dict, f, indent=2)
        print(f"\n  💾 SHAP importance saved: {importance_file}")

        return selected

    except Exception as e:
        print(f"\n  ⚠️ SHAP analysis failed: {e}")
        print(f"  → Keeping all {len(features)} features")
        return features


# ============================================================================
# PART 7: SAVE MODEL FOR PRODUCTION
# ============================================================================

def save_model(model: xgb.XGBClassifier, features: List[str],
               threshold: float, params: Dict, metrics: Dict,
               wf_metrics: Dict):
    """Save model and all artifacts for production."""
    print(f"\n{'='*70}")
    print("💾 SAVING MODEL FOR PRODUCTION")
    print(f"{'='*70}")

    # Save XGBoost model
    model_path = os.path.join(MODELS_DIR, "xgboost_model.json")
    model.save_model(model_path)
    print(f"  ✅ Model: {model_path}")

    # Save feature list
    features_path = os.path.join(MODELS_DIR, "xgboost_features.json")
    with open(features_path, 'w') as f:
        json.dump(features, f, indent=2)
    print(f"  ✅ Features: {features_path}")

    # Save config
    config = {
        'model_type': 'xgboost',
        'model_file': 'xgboost_model.json',
        'features_file': 'xgboost_features.json',
        'n_features': len(features),
        'label_threshold_pct': threshold,
        'prediction_horizon': 1,  # 1 H1 bar ahead
        'xgboost_params': params,
        'probability_percentiles': metrics.get('probability_percentiles', {}),
        'holdout_metrics': {k: round(v, 6) for k, v in metrics.items() if k != 'probability_percentiles'},
        'walk_forward_metrics': {k: round(v, 6) for k, v in wf_metrics.items()},
        'trained_at': datetime.now().isoformat(),
        'training_data': DATASET_FILE,
    }

    config_path = os.path.join(MODELS_DIR, "xgboost_config.json")
    with open(config_path, 'w') as f:
        json.dump(config, f, indent=2)
    print(f"  ✅ Config: {config_path}")


# ============================================================================
# MAIN PIPELINE
# ============================================================================

def main():
    print("=" * 70)
    print("🧠 XGBOOST TRAINING PIPELINE — XAUUSD")
    print("=" * 70)
    print(f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print()

    # --- Step 1: Load data ---
    df = load_dataset()

    # --- Step 2: Multi-threshold testing ---
    best_threshold, labels = test_thresholds(df, FEATURE_COLUMNS)

    # --- Step 3: Optuna tuning ---
    best_params = run_optuna_tuning(df, FEATURE_COLUMNS, labels, n_trials=100)

    # --- Step 4: Walk-forward with best params (full verbose) ---
    print(f"\n{'='*70}")
    print("📊 WALK-FORWARD WITH BEST PARAMS")
    print(f"{'='*70}")
    wf_metrics = walk_forward_evaluate(df, FEATURE_COLUMNS, labels, best_params, verbose=True)
    print(f"\n  Avg AUC: {wf_metrics['auc']:.4f} ± {wf_metrics['auc_std']:.4f}")
    print(f"  Overall AUC: {wf_metrics['auc_overall']:.4f}")
    print(f"  Avg Accuracy: {wf_metrics['accuracy']:.4f} ± {wf_metrics['accuracy_std']:.4f}")
    print(f"  Avg Precision: {wf_metrics['precision']:.4f} ± {wf_metrics['precision_std']:.4f}")
    print(f"  Avg Recall: {wf_metrics['recall']:.4f} ± {wf_metrics['recall_std']:.4f}")

    # --- Step 5: Train final model (all features) ---
    model_full, metrics_full = train_final_model(df, FEATURE_COLUMNS, labels, best_params)

    # --- Step 6: SHAP analysis ---
    selected_features = run_shap_analysis(model_full, df, FEATURE_COLUMNS, labels)

    # --- Step 7: Retrain with selected features if different ---
    if len(selected_features) < len(FEATURE_COLUMNS):
        print(f"\n{'='*70}")
        print(f"🔄 RETRAINING WITH {len(selected_features)} SELECTED FEATURES")
        print(f"{'='*70}")

        # Walk-forward with selected features
        wf_selected = walk_forward_evaluate(df, selected_features, labels, best_params, verbose=True)
        print(f"\n  Selected features AUC: {wf_selected['auc_overall']:.4f} "
              f"(was {wf_metrics['auc_overall']:.4f} with all {len(FEATURE_COLUMNS)})")

        # Use selected features only if AUC improved or stayed within 0.005
        if wf_selected['auc_overall'] >= wf_metrics['auc_overall'] - 0.005:
            print(f"  ✅ Using {len(selected_features)} selected features (AUC maintained/improved)")
            final_features = selected_features
            model_final, metrics_final = train_final_model(df, selected_features, labels, best_params)
            wf_final = wf_selected
        else:
            print(f"  ⚠️ Selected features AUC dropped too much. Keeping all {len(FEATURE_COLUMNS)} features.")
            final_features = FEATURE_COLUMNS
            model_final = model_full
            metrics_final = metrics_full
            wf_final = wf_metrics
    else:
        print(f"\n  All features have >= 1% importance. Keeping all {len(FEATURE_COLUMNS)}.")
        final_features = FEATURE_COLUMNS
        model_final = model_full
        metrics_final = metrics_full
        wf_final = wf_metrics

    # --- Step 8: Save ---
    save_model(model_final, final_features, best_threshold, best_params, metrics_final, wf_final)

    # --- Summary ---
    print(f"\n{'='*70}")
    print("📊 FINAL SUMMARY")
    print(f"{'='*70}")
    print(f"""
  Model:          XGBoost
  Features:       {len(final_features)} (from {len(FEATURE_COLUMNS)} original)
  Label threshold: {best_threshold}%
  
  Walk-Forward (avg across folds):
    AUC:       {wf_final['auc']:.4f} ± {wf_final['auc_std']:.4f}
    AUC overall: {wf_final['auc_overall']:.4f}
    Accuracy:  {wf_final['accuracy']:.4f} ± {wf_final['accuracy_std']:.4f}
    Precision: {wf_final['precision']:.4f} ± {wf_final['precision_std']:.4f}
    Recall:    {wf_final['recall']:.4f} ± {wf_final['recall_std']:.4f}
  
  Holdout (last 2 months):
    AUC:       {metrics_final['auc']:.4f}
    Accuracy:  {metrics_final['accuracy']:.4f}
    Precision: {metrics_final['precision']:.4f}
    Recall:    {metrics_final['recall']:.4f}
    F1:        {metrics_final['f1']:.4f}
  
  Files saved:
    models/xgboost_model.json
    models/xgboost_features.json
    models/xgboost_config.json
    models/shap_importance.json
""")

    # Compare with old model
    old_config_path = os.path.join(MODELS_DIR, "model_config.json")
    if os.path.exists(old_config_path):
        with open(old_config_path) as f:
            old = json.load(f)
        old_auc = old.get('metrics', {}).get('auc', 0)
        old_acc = old.get('metrics', {}).get('accuracy', 0)
        print(f"  📈 vs Old Model (LSTM/Gradient Boost):")
        print(f"     Old AUC: {old_auc:.4f} → New AUC: {metrics_final['auc']:.4f} ({(metrics_final['auc']-old_auc)*100:+.2f}%)")
        print(f"     Old Acc: {old_acc:.4f} → New Acc: {metrics_final['accuracy']:.4f} ({(metrics_final['accuracy']-old_acc)*100:+.2f}%)")

    print(f"\n{'='*70}")
    print("✅ TRAINING COMPLETE!")
    print(f"{'='*70}")


if __name__ == "__main__":
    main()
