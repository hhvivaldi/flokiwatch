"""
FINAL TEST: Dataset Balancing for ML
Tests 3 approaches: Class Weights, SMOTE, Undersampling
"""

import os
import numpy as np
import pandas as pd
from datetime import datetime
import warnings
warnings.filterwarnings('ignore')

# ML Libraries
from sklearn.preprocessing import MinMaxScaler
from sklearn.metrics import (
    accuracy_score, precision_score, recall_score, f1_score,
    roc_auc_score, confusion_matrix, classification_report
)
from sklearn.ensemble import RandomForestClassifier, GradientBoostingClassifier
from sklearn.utils.class_weight import compute_class_weight
import joblib

# Balancing
from imblearn.over_sampling import SMOTE
from imblearn.under_sampling import RandomUnderSampler

# TensorFlow/Keras
import tensorflow as tf
from tensorflow import keras
from keras.models import Sequential
from keras.layers import LSTM, Dense, Dropout, Input, Bidirectional
from keras.callbacks import EarlyStopping, ReduceLROnPlateau
from keras.optimizers import Adam

# Configuration
DATA_DIR = "data"
MODELS_DIR = "models"
SYMBOL = "XAUUSD"

# Fixed hyperparameters
SEQ_LENGTH = 60
HORIZON = 4
THRESHOLD = 0.001

os.makedirs(MODELS_DIR, exist_ok=True)


def load_and_prepare_data():
    """Load and prepare data with features"""
    filepath = os.path.join(DATA_DIR, f"{SYMBOL}_H1_with_indicators.csv")
    df = pd.read_csv(filepath, parse_dates=['datetime'])
    df = df.sort_values('datetime').reset_index(drop=True)
    
    # Temporal features
    df['hour'] = df['datetime'].dt.hour
    df['day_of_week'] = df['datetime'].dt.dayofweek
    
    # Price changes
    df['price_change_1h'] = df['close'].pct_change(1) * 100
    df['price_change_4h'] = df['close'].pct_change(4) * 100
    df['price_change_24h'] = df['close'].pct_change(24) * 100
    
    # Volatility
    df['volatility_4h'] = (df['high'].rolling(4).max() - df['low'].rolling(4).min()) / df['close'] * 100
    df['volatility_24h'] = (df['high'].rolling(24).max() - df['low'].rolling(24).min()) / df['close'] * 100
    
    # Distance from EMAs
    df['dist_ema9'] = (df['close'] - df['ema_9']) / df['close'] * 100
    df['dist_ema21'] = (df['close'] - df['ema_21']) / df['close'] * 100
    df['dist_ema50'] = (df['close'] - df['ema_50']) / df['close'] * 100
    
    # Bollinger position
    bb_range = df['bb_upper'] - df['bb_lower']
    df['bb_position'] = np.where(bb_range > 0, (df['close'] - df['bb_lower']) / bb_range * 100, 50)
    
    # Momentum
    df['macd_momentum'] = df['macd_hist'].diff()
    df['rsi_momentum'] = df['rsi_14'].diff()
    
    return df


def get_feature_columns():
    return [
        'open', 'high', 'low', 'close', 'volume',
        'ema_9', 'ema_21', 'ema_50',
        'rsi_14',
        'macd', 'macd_signal', 'macd_hist',
        'bb_upper', 'bb_middle', 'bb_lower',
        'hour', 'day_of_week',
        'price_change_1h', 'price_change_4h', 'price_change_24h',
        'volatility_4h', 'volatility_24h',
        'dist_ema9', 'dist_ema21', 'dist_ema50',
        'bb_position',
        'macd_momentum', 'rsi_momentum',
    ]


def create_labels(df):
    """Create labels for classification"""
    df = df.copy()
    df['future_close'] = df['close'].shift(-HORIZON)
    df['future_change'] = (df['future_close'] - df['close']) / df['close']
    df['label'] = (df['future_change'] > THRESHOLD).astype(int)
    return df.dropna(subset=['label'])


def create_sequences(data, labels):
    """Create sequences for LSTM"""
    X, y = [], []
    for i in range(SEQ_LENGTH, len(data)):
        X.append(data[i-SEQ_LENGTH:i])
        y.append(labels[i])
    return np.array(X), np.array(y)


def temporal_split(X, y, train_ratio=0.70, val_ratio=0.15):
    """Temporal split - NEVER random!"""
    n = len(X)
    train_end = int(n * train_ratio)
    val_end = int(n * (train_ratio + val_ratio))
    
    return (
        X[:train_end], X[train_end:val_end], X[val_end:],
        y[:train_end], y[train_end:val_end], y[val_end:]
    )


def calculate_class_weights(y):
    """Calculate inversely proportional class weights"""
    classes = np.unique(y)
    weights = compute_class_weight('balanced', classes=classes, y=y)
    return dict(zip(classes, weights))


def print_detailed_metrics(y_true, y_pred, y_pred_proba, model_name):
    """Print detailed metrics for both classes"""
    acc = accuracy_score(y_true, y_pred)
    
    # Precision and Recall per class
    prec_0 = precision_score(y_true, y_pred, pos_label=0, zero_division=0)
    prec_1 = precision_score(y_true, y_pred, pos_label=1, zero_division=0)
    rec_0 = recall_score(y_true, y_pred, pos_label=0, zero_division=0)
    rec_1 = recall_score(y_true, y_pred, pos_label=1, zero_division=0)
    f1 = f1_score(y_true, y_pred, zero_division=0)
    
    try:
        auc = roc_auc_score(y_true, y_pred_proba)
    except:
        auc = 0.5
    
    cm = confusion_matrix(y_true, y_pred)
    
    return {
        'accuracy': acc,
        'precision_0': prec_0,
        'precision_1': prec_1,
        'recall_0': rec_0,
        'recall_1': rec_1,
        'f1': f1,
        'auc': auc,
        'confusion_matrix': cm,
    }


# ============================================================================
# MODELS
# ============================================================================

def train_gradient_boost(X_train, y_train, class_weight=None):
    """Train Gradient Boosting with class weights"""
    X_flat = X_train.reshape(X_train.shape[0], -1)
    
    # Calculate scale_pos_weight if class_weight provided
    if class_weight:
        # GradientBoostingClassifier doesn't support class_weight directly
        # We'll use sample_weight
        sample_weights = np.array([class_weight[y] for y in y_train])
    else:
        sample_weights = None
    
    model = GradientBoostingClassifier(
        n_estimators=100,
        max_depth=5,
        learning_rate=0.1,
        subsample=0.8,
        random_state=42
    )
    model.fit(X_flat, y_train, sample_weight=sample_weights)
    return model


def train_random_forest(X_train, y_train, class_weight=None):
    """Train Random Forest with class weights"""
    X_flat = X_train.reshape(X_train.shape[0], -1)
    
    model = RandomForestClassifier(
        n_estimators=200,
        max_depth=10,
        min_samples_split=10,
        class_weight=class_weight,  # RF supports directly
        random_state=42,
        n_jobs=-1
    )
    model.fit(X_flat, y_train)
    return model


def train_lstm(X_train, y_train, X_val, y_val, class_weight=None, bidirectional=False):
    """Train LSTM with class weights"""
    model = Sequential()
    model.add(Input(shape=(X_train.shape[1], X_train.shape[2])))
    
    if bidirectional:
        model.add(Bidirectional(LSTM(128, return_sequences=True)))
    else:
        model.add(LSTM(128, return_sequences=True, recurrent_dropout=0.1))
    model.add(Dropout(0.3))
    
    if bidirectional:
        model.add(Bidirectional(LSTM(64)))
    else:
        model.add(LSTM(64, recurrent_dropout=0.1))
    model.add(Dropout(0.3))
    
    model.add(Dense(32, activation='relu'))
    model.add(Dropout(0.2))
    model.add(Dense(16, activation='relu'))
    model.add(Dense(1, activation='sigmoid'))
    
    model.compile(
        optimizer=Adam(learning_rate=0.0005),
        loss='binary_crossentropy',
        metrics=['accuracy']
    )
    
    callbacks = [
        EarlyStopping(monitor='val_loss', patience=10, restore_best_weights=True),
        ReduceLROnPlateau(monitor='val_loss', factor=0.5, patience=5, min_lr=0.00001)
    ]
    
    model.fit(
        X_train, y_train,
        validation_data=(X_val, y_val),
        epochs=50,
        batch_size=32,
        callbacks=callbacks,
        class_weight=class_weight,
        verbose=0
    )
    return model


def evaluate_model(model, X_test, y_test, is_lstm=True):
    """Evaluate model and return predictions"""
    if is_lstm:
        y_pred_proba = model.predict(X_test, verbose=0).flatten()
    else:
        X_flat = X_test.reshape(X_test.shape[0], -1)
        y_pred_proba = model.predict_proba(X_flat)[:, 1]
    
    y_pred = (y_pred_proba > 0.5).astype(int)
    return y_pred, y_pred_proba


# ============================================================================
# BALANCING TESTS
# ============================================================================

def run_baseline_test(X_train, y_train, X_val, y_val, X_test, y_test):
    """Baseline test WITHOUT balancing"""
    print("\n" + "="*80)
    print("🧪 BASELINE TEST (NO BALANCING)")
    print("="*80)
    
    results = {}
    
    # Gradient Boost
    print("\n📈 GRADIENT BOOST:")
    model = train_gradient_boost(X_train, y_train, class_weight=None)
    y_pred_train, y_proba_train = evaluate_model(model, X_train, y_train, is_lstm=False)
    y_pred_test, y_proba_test = evaluate_model(model, X_test, y_test, is_lstm=False)
    
    train_m = print_detailed_metrics(y_train, y_pred_train, y_proba_train, "GB")
    test_m = print_detailed_metrics(y_test, y_pred_test, y_proba_test, "GB")
    
    print(f"   Train Acc: {train_m['accuracy']*100:.2f}%")
    print(f"   Test Acc:  {test_m['accuracy']*100:.2f}%")
    print(f"   Overfit:   {(train_m['accuracy']-test_m['accuracy'])*100:+.2f}%")
    print(f"   Precision (bearish/0): {test_m['precision_0']*100:.2f}%")
    print(f"   Precision (bullish/1):  {test_m['precision_1']*100:.2f}%")
    print(f"   Recall (bearish/0):    {test_m['recall_0']*100:.2f}%")
    print(f"   Recall (bullish/1):     {test_m['recall_1']*100:.2f}%")
    print(f"   AUC: {test_m['auc']:.4f}")
    print(f"   Confusion Matrix:\n   {test_m['confusion_matrix']}")
    
    results['gradient_boost'] = {'train': train_m, 'test': test_m}
    
    # Random Forest
    print("\n📈 RANDOM FOREST:")
    model = train_random_forest(X_train, y_train, class_weight=None)
    y_pred_train, y_proba_train = evaluate_model(model, X_train, y_train, is_lstm=False)
    y_pred_test, y_proba_test = evaluate_model(model, X_test, y_test, is_lstm=False)
    
    train_m = print_detailed_metrics(y_train, y_pred_train, y_proba_train, "RF")
    test_m = print_detailed_metrics(y_test, y_pred_test, y_proba_test, "RF")
    
    print(f"   Train Acc: {train_m['accuracy']*100:.2f}%")
    print(f"   Test Acc:  {test_m['accuracy']*100:.2f}%")
    print(f"   Overfit:   {(train_m['accuracy']-test_m['accuracy'])*100:+.2f}%")
    print(f"   Precision (bearish/0): {test_m['precision_0']*100:.2f}%")
    print(f"   Precision (bullish/1):  {test_m['precision_1']*100:.2f}%")
    print(f"   Recall (bearish/0):    {test_m['recall_0']*100:.2f}%")
    print(f"   Recall (bullish/1):     {test_m['recall_1']*100:.2f}%")
    print(f"   AUC: {test_m['auc']:.4f}")
    print(f"   Confusion Matrix:\n   {test_m['confusion_matrix']}")
    
    results['random_forest'] = {'train': train_m, 'test': test_m}
    
    # LSTM
    print("\n📈 LSTM:")
    model = train_lstm(X_train, y_train, X_val, y_val, class_weight=None, bidirectional=False)
    y_pred_train, y_proba_train = evaluate_model(model, X_train, y_train, is_lstm=True)
    y_pred_test, y_proba_test = evaluate_model(model, X_test, y_test, is_lstm=True)
    
    train_m = print_detailed_metrics(y_train, y_pred_train, y_proba_train, "LSTM")
    test_m = print_detailed_metrics(y_test, y_pred_test, y_proba_test, "LSTM")
    
    print(f"   Train Acc: {train_m['accuracy']*100:.2f}%")
    print(f"   Test Acc:  {test_m['accuracy']*100:.2f}%")
    print(f"   Overfit:   {(train_m['accuracy']-test_m['accuracy'])*100:+.2f}%")
    print(f"   Precision (bearish/0): {test_m['precision_0']*100:.2f}%")
    print(f"   Precision (bullish/1):  {test_m['precision_1']*100:.2f}%")
    print(f"   Recall (bearish/0):    {test_m['recall_0']*100:.2f}%")
    print(f"   Recall (bullish/1):     {test_m['recall_1']*100:.2f}%")
    print(f"   AUC: {test_m['auc']:.4f}")
    print(f"   Confusion Matrix:\n   {test_m['confusion_matrix']}")
    
    results['lstm'] = {'train': train_m, 'test': test_m}
    
    # BiLSTM
    print("\n📈 BiLSTM:")
    model = train_lstm(X_train, y_train, X_val, y_val, class_weight=None, bidirectional=True)
    y_pred_train, y_proba_train = evaluate_model(model, X_train, y_train, is_lstm=True)
    y_pred_test, y_proba_test = evaluate_model(model, X_test, y_test, is_lstm=True)
    
    train_m = print_detailed_metrics(y_train, y_pred_train, y_proba_train, "BiLSTM")
    test_m = print_detailed_metrics(y_test, y_pred_test, y_proba_test, "BiLSTM")
    
    print(f"   Train Acc: {train_m['accuracy']*100:.2f}%")
    print(f"   Test Acc:  {test_m['accuracy']*100:.2f}%")
    print(f"   Overfit:   {(train_m['accuracy']-test_m['accuracy'])*100:+.2f}%")
    print(f"   Precision (bearish/0): {test_m['precision_0']*100:.2f}%")
    print(f"   Precision (bullish/1):  {test_m['precision_1']*100:.2f}%")
    print(f"   Recall (bearish/0):    {test_m['recall_0']*100:.2f}%")
    print(f"   Recall (bullish/1):     {test_m['recall_1']*100:.2f}%")
    print(f"   AUC: {test_m['auc']:.4f}")
    print(f"   Confusion Matrix:\n   {test_m['confusion_matrix']}")
    
    results['bilstm'] = {'train': train_m, 'test': test_m}
    
    return results


def run_class_weights_test(X_train, y_train, X_val, y_val, X_test, y_test):
    """Test with CLASS WEIGHTS"""
    print("\n" + "="*80)
    print("🧪 TEST PHASE 1: CLASS WEIGHTS")
    print("="*80)
    
    # Calculate class weights
    class_weight = calculate_class_weights(y_train)
    print(f"\n📊 Class Weights calculated:")
    print(f"   Class 0 (bearish): {class_weight[0]:.4f}")
    print(f"   Class 1 (bullish):  {class_weight[1]:.4f}")
    
    results = {}
    
    # Gradient Boost
    print("\n📈 GRADIENT BOOST + Class Weights:")
    model = train_gradient_boost(X_train, y_train, class_weight=class_weight)
    y_pred_train, y_proba_train = evaluate_model(model, X_train, y_train, is_lstm=False)
    y_pred_test, y_proba_test = evaluate_model(model, X_test, y_test, is_lstm=False)
    
    train_m = print_detailed_metrics(y_train, y_pred_train, y_proba_train, "GB")
    test_m = print_detailed_metrics(y_test, y_pred_test, y_proba_test, "GB")
    
    print(f"   Train Acc: {train_m['accuracy']*100:.2f}%")
    print(f"   Test Acc:  {test_m['accuracy']*100:.2f}%")
    print(f"   Overfit:   {(train_m['accuracy']-test_m['accuracy'])*100:+.2f}%")
    print(f"   Precision (bearish/0): {test_m['precision_0']*100:.2f}%")
    print(f"   Precision (bullish/1):  {test_m['precision_1']*100:.2f}%")
    print(f"   Recall (bearish/0):    {test_m['recall_0']*100:.2f}%")
    print(f"   Recall (bullish/1):     {test_m['recall_1']*100:.2f}%")
    print(f"   AUC: {test_m['auc']:.4f}")
    print(f"   Confusion Matrix:\n   {test_m['confusion_matrix']}")
    
    results['gradient_boost'] = {'train': train_m, 'test': test_m, 'model': model}
    
    # Random Forest
    print("\n📈 RANDOM FOREST + Class Weights:")
    model = train_random_forest(X_train, y_train, class_weight='balanced')
    y_pred_train, y_proba_train = evaluate_model(model, X_train, y_train, is_lstm=False)
    y_pred_test, y_proba_test = evaluate_model(model, X_test, y_test, is_lstm=False)
    
    train_m = print_detailed_metrics(y_train, y_pred_train, y_proba_train, "RF")
    test_m = print_detailed_metrics(y_test, y_pred_test, y_proba_test, "RF")
    
    print(f"   Train Acc: {train_m['accuracy']*100:.2f}%")
    print(f"   Test Acc:  {test_m['accuracy']*100:.2f}%")
    print(f"   Overfit:   {(train_m['accuracy']-test_m['accuracy'])*100:+.2f}%")
    print(f"   Precision (bearish/0): {test_m['precision_0']*100:.2f}%")
    print(f"   Precision (bullish/1):  {test_m['precision_1']*100:.2f}%")
    print(f"   Recall (bearish/0):    {test_m['recall_0']*100:.2f}%")
    print(f"   Recall (bullish/1):     {test_m['recall_1']*100:.2f}%")
    print(f"   AUC: {test_m['auc']:.4f}")
    print(f"   Confusion Matrix:\n   {test_m['confusion_matrix']}")
    
    results['random_forest'] = {'train': train_m, 'test': test_m, 'model': model}
    
    # LSTM
    print("\n📈 LSTM + Class Weights:")
    model = train_lstm(X_train, y_train, X_val, y_val, class_weight=class_weight, bidirectional=False)
    y_pred_train, y_proba_train = evaluate_model(model, X_train, y_train, is_lstm=True)
    y_pred_test, y_proba_test = evaluate_model(model, X_test, y_test, is_lstm=True)
    
    train_m = print_detailed_metrics(y_train, y_pred_train, y_proba_train, "LSTM")
    test_m = print_detailed_metrics(y_test, y_pred_test, y_proba_test, "LSTM")
    
    print(f"   Train Acc: {train_m['accuracy']*100:.2f}%")
    print(f"   Test Acc:  {test_m['accuracy']*100:.2f}%")
    print(f"   Overfit:   {(train_m['accuracy']-test_m['accuracy'])*100:+.2f}%")
    print(f"   Precision (bearish/0): {test_m['precision_0']*100:.2f}%")
    print(f"   Precision (bullish/1):  {test_m['precision_1']*100:.2f}%")
    print(f"   Recall (bearish/0):    {test_m['recall_0']*100:.2f}%")
    print(f"   Recall (bullish/1):     {test_m['recall_1']*100:.2f}%")
    print(f"   AUC: {test_m['auc']:.4f}")
    print(f"   Confusion Matrix:\n   {test_m['confusion_matrix']}")
    
    results['lstm'] = {'train': train_m, 'test': test_m, 'model': model}
    
    # BiLSTM
    print("\n📈 BiLSTM + Class Weights:")
    model = train_lstm(X_train, y_train, X_val, y_val, class_weight=class_weight, bidirectional=True)
    y_pred_train, y_proba_train = evaluate_model(model, X_train, y_train, is_lstm=True)
    y_pred_test, y_proba_test = evaluate_model(model, X_test, y_test, is_lstm=True)
    
    train_m = print_detailed_metrics(y_train, y_pred_train, y_proba_train, "BiLSTM")
    test_m = print_detailed_metrics(y_test, y_pred_test, y_proba_test, "BiLSTM")
    
    print(f"   Train Acc: {train_m['accuracy']*100:.2f}%")
    print(f"   Test Acc:  {test_m['accuracy']*100:.2f}%")
    print(f"   Overfit:   {(train_m['accuracy']-test_m['accuracy'])*100:+.2f}%")
    print(f"   Precision (bearish/0): {test_m['precision_0']*100:.2f}%")
    print(f"   Precision (bullish/1):  {test_m['precision_1']*100:.2f}%")
    print(f"   Recall (bearish/0):    {test_m['recall_0']*100:.2f}%")
    print(f"   Recall (bullish/1):     {test_m['recall_1']*100:.2f}%")
    print(f"   AUC: {test_m['auc']:.4f}")
    print(f"   Confusion Matrix:\n   {test_m['confusion_matrix']}")
    
    results['bilstm'] = {'train': train_m, 'test': test_m, 'model': model}
    
    return results


def run_smote_test(X_train, y_train, X_val, y_val, X_test, y_test):
    """Test with SMOTE (training only!)"""
    print("\n" + "="*80)
    print("🧪 TEST PHASE 2: SMOTE (Synthetic Minority Over-sampling)")
    print("="*80)
    
    # Flatten for SMOTE
    X_train_flat = X_train.reshape(X_train.shape[0], -1)
    
    print(f"\n📊 Before SMOTE:")
    print(f"   Class 0 (bearish): {(y_train == 0).sum()} ({(y_train == 0).mean()*100:.1f}%)")
    print(f"   Class 1 (bullish):  {(y_train == 1).sum()} ({(y_train == 1).mean()*100:.1f}%)")
    
    # Apply SMOTE
    smote = SMOTE(random_state=42)
    X_train_smote, y_train_smote = smote.fit_resample(X_train_flat, y_train)
    
    print(f"\n📊 After SMOTE:")
    print(f"   Class 0 (bearish): {(y_train_smote == 0).sum()} ({(y_train_smote == 0).mean()*100:.1f}%)")
    print(f"   Class 1 (bullish):  {(y_train_smote == 1).sum()} ({(y_train_smote == 1).mean()*100:.1f}%)")
    
    # Reshape back for LSTM
    n_samples = X_train_smote.shape[0]
    X_train_smote_3d = X_train_smote.reshape(n_samples, SEQ_LENGTH, -1)
    
    results = {}
    
    # Gradient Boost (usa flat)
    print("\n📈 GRADIENT BOOST + SMOTE:")
    model = GradientBoostingClassifier(n_estimators=100, max_depth=5, learning_rate=0.1, subsample=0.8, random_state=42)
    model.fit(X_train_smote, y_train_smote)
    
    y_pred_train = model.predict(X_train_smote)
    y_proba_train = model.predict_proba(X_train_smote)[:, 1]
    X_test_flat = X_test.reshape(X_test.shape[0], -1)
    y_pred_test = model.predict(X_test_flat)
    y_proba_test = model.predict_proba(X_test_flat)[:, 1]
    
    train_m = print_detailed_metrics(y_train_smote, y_pred_train, y_proba_train, "GB")
    test_m = print_detailed_metrics(y_test, y_pred_test, y_proba_test, "GB")
    
    print(f"   Train Acc: {train_m['accuracy']*100:.2f}%")
    print(f"   Test Acc:  {test_m['accuracy']*100:.2f}%")
    print(f"   Overfit:   {(train_m['accuracy']-test_m['accuracy'])*100:+.2f}%")
    print(f"   Precision (bearish/0): {test_m['precision_0']*100:.2f}%")
    print(f"   Precision (bullish/1):  {test_m['precision_1']*100:.2f}%")
    print(f"   Recall (bearish/0):    {test_m['recall_0']*100:.2f}%")
    print(f"   Recall (bullish/1):     {test_m['recall_1']*100:.2f}%")
    print(f"   AUC: {test_m['auc']:.4f}")
    print(f"   Confusion Matrix:\n   {test_m['confusion_matrix']}")
    
    results['gradient_boost'] = {'train': train_m, 'test': test_m}
    
    # Random Forest
    print("\n📈 RANDOM FOREST + SMOTE:")
    model = RandomForestClassifier(n_estimators=200, max_depth=10, min_samples_split=10, random_state=42, n_jobs=-1)
    model.fit(X_train_smote, y_train_smote)
    
    y_pred_train = model.predict(X_train_smote)
    y_proba_train = model.predict_proba(X_train_smote)[:, 1]
    y_pred_test = model.predict(X_test_flat)
    y_proba_test = model.predict_proba(X_test_flat)[:, 1]
    
    train_m = print_detailed_metrics(y_train_smote, y_pred_train, y_proba_train, "RF")
    test_m = print_detailed_metrics(y_test, y_pred_test, y_proba_test, "RF")
    
    print(f"   Train Acc: {train_m['accuracy']*100:.2f}%")
    print(f"   Test Acc:  {test_m['accuracy']*100:.2f}%")
    print(f"   Overfit:   {(train_m['accuracy']-test_m['accuracy'])*100:+.2f}%")
    print(f"   Precision (bearish/0): {test_m['precision_0']*100:.2f}%")
    print(f"   Precision (bullish/1):  {test_m['precision_1']*100:.2f}%")
    print(f"   Recall (bearish/0):    {test_m['recall_0']*100:.2f}%")
    print(f"   Recall (bullish/1):     {test_m['recall_1']*100:.2f}%")
    print(f"   AUC: {test_m['auc']:.4f}")
    print(f"   Confusion Matrix:\n   {test_m['confusion_matrix']}")
    
    results['random_forest'] = {'train': train_m, 'test': test_m}
    
    # LSTM
    print("\n📈 LSTM + SMOTE:")
    model = train_lstm(X_train_smote_3d, y_train_smote, X_val, y_val, class_weight=None, bidirectional=False)
    
    y_proba_train = model.predict(X_train_smote_3d, verbose=0).flatten()
    y_pred_train = (y_proba_train > 0.5).astype(int)
    y_proba_test = model.predict(X_test, verbose=0).flatten()
    y_pred_test = (y_proba_test > 0.5).astype(int)
    
    train_m = print_detailed_metrics(y_train_smote, y_pred_train, y_proba_train, "LSTM")
    test_m = print_detailed_metrics(y_test, y_pred_test, y_proba_test, "LSTM")
    
    print(f"   Train Acc: {train_m['accuracy']*100:.2f}%")
    print(f"   Test Acc:  {test_m['accuracy']*100:.2f}%")
    print(f"   Overfit:   {(train_m['accuracy']-test_m['accuracy'])*100:+.2f}%")
    print(f"   Precision (bearish/0): {test_m['precision_0']*100:.2f}%")
    print(f"   Precision (bullish/1):  {test_m['precision_1']*100:.2f}%")
    print(f"   Recall (bearish/0):    {test_m['recall_0']*100:.2f}%")
    print(f"   Recall (bullish/1):     {test_m['recall_1']*100:.2f}%")
    print(f"   AUC: {test_m['auc']:.4f}")
    print(f"   Confusion Matrix:\n   {test_m['confusion_matrix']}")
    
    results['lstm'] = {'train': train_m, 'test': test_m}
    
    # BiLSTM
    print("\n📈 BiLSTM + SMOTE:")
    model = train_lstm(X_train_smote_3d, y_train_smote, X_val, y_val, class_weight=None, bidirectional=True)
    
    y_proba_train = model.predict(X_train_smote_3d, verbose=0).flatten()
    y_pred_train = (y_proba_train > 0.5).astype(int)
    y_proba_test = model.predict(X_test, verbose=0).flatten()
    y_pred_test = (y_proba_test > 0.5).astype(int)
    
    train_m = print_detailed_metrics(y_train_smote, y_pred_train, y_proba_train, "BiLSTM")
    test_m = print_detailed_metrics(y_test, y_pred_test, y_proba_test, "BiLSTM")
    
    print(f"   Train Acc: {train_m['accuracy']*100:.2f}%")
    print(f"   Test Acc:  {test_m['accuracy']*100:.2f}%")
    print(f"   Overfit:   {(train_m['accuracy']-test_m['accuracy'])*100:+.2f}%")
    print(f"   Precision (bearish/0): {test_m['precision_0']*100:.2f}%")
    print(f"   Precision (bullish/1):  {test_m['precision_1']*100:.2f}%")
    print(f"   Recall (bearish/0):    {test_m['recall_0']*100:.2f}%")
    print(f"   Recall (bullish/1):     {test_m['recall_1']*100:.2f}%")
    print(f"   AUC: {test_m['auc']:.4f}")
    print(f"   Confusion Matrix:\n   {test_m['confusion_matrix']}")
    
    results['bilstm'] = {'train': train_m, 'test': test_m}
    
    return results


def run_undersampling_test(X_train, y_train, X_val, y_val, X_test, y_test):
    """Test with Undersampling (training only!)"""
    print("\n" + "="*80)
    print("🧪 TEST PHASE 3: UNDERSAMPLING (Remove majority excess)")
    print("="*80)
    
    # Flatten for undersampling
    X_train_flat = X_train.reshape(X_train.shape[0], -1)
    
    print(f"\n📊 Before Undersampling:")
    print(f"   Class 0 (bearish): {(y_train == 0).sum()} ({(y_train == 0).mean()*100:.1f}%)")
    print(f"   Class 1 (bullish):  {(y_train == 1).sum()} ({(y_train == 1).mean()*100:.1f}%)")
    
    # Apply Undersampling
    rus = RandomUnderSampler(random_state=42)
    X_train_under, y_train_under = rus.fit_resample(X_train_flat, y_train)
    
    print(f"\n📊 After Undersampling:")
    print(f"   Class 0 (bearish): {(y_train_under == 0).sum()} ({(y_train_under == 0).mean()*100:.1f}%)")
    print(f"   Class 1 (bullish):  {(y_train_under == 1).sum()} ({(y_train_under == 1).mean()*100:.1f}%)")
    print(f"   ⚠️ Dataset reduced from {len(y_train)} to {len(y_train_under)} samples")
    
    # Reshape back for LSTM
    n_samples = X_train_under.shape[0]
    X_train_under_3d = X_train_under.reshape(n_samples, SEQ_LENGTH, -1)
    
    results = {}
    
    # Gradient Boost
    print("\n📈 GRADIENT BOOST + Undersampling:")
    model = GradientBoostingClassifier(n_estimators=100, max_depth=5, learning_rate=0.1, subsample=0.8, random_state=42)
    model.fit(X_train_under, y_train_under)
    
    y_pred_train = model.predict(X_train_under)
    y_proba_train = model.predict_proba(X_train_under)[:, 1]
    X_test_flat = X_test.reshape(X_test.shape[0], -1)
    y_pred_test = model.predict(X_test_flat)
    y_proba_test = model.predict_proba(X_test_flat)[:, 1]
    
    train_m = print_detailed_metrics(y_train_under, y_pred_train, y_proba_train, "GB")
    test_m = print_detailed_metrics(y_test, y_pred_test, y_proba_test, "GB")
    
    print(f"   Train Acc: {train_m['accuracy']*100:.2f}%")
    print(f"   Test Acc:  {test_m['accuracy']*100:.2f}%")
    print(f"   Overfit:   {(train_m['accuracy']-test_m['accuracy'])*100:+.2f}%")
    print(f"   Precision (bearish/0): {test_m['precision_0']*100:.2f}%")
    print(f"   Precision (bullish/1):  {test_m['precision_1']*100:.2f}%")
    print(f"   Recall (bearish/0):    {test_m['recall_0']*100:.2f}%")
    print(f"   Recall (bullish/1):     {test_m['recall_1']*100:.2f}%")
    print(f"   AUC: {test_m['auc']:.4f}")
    print(f"   Confusion Matrix:\n   {test_m['confusion_matrix']}")
    
    results['gradient_boost'] = {'train': train_m, 'test': test_m}
    
    # Random Forest
    print("\n📈 RANDOM FOREST + Undersampling:")
    model = RandomForestClassifier(n_estimators=200, max_depth=10, min_samples_split=10, random_state=42, n_jobs=-1)
    model.fit(X_train_under, y_train_under)
    
    y_pred_train = model.predict(X_train_under)
    y_proba_train = model.predict_proba(X_train_under)[:, 1]
    y_pred_test = model.predict(X_test_flat)
    y_proba_test = model.predict_proba(X_test_flat)[:, 1]
    
    train_m = print_detailed_metrics(y_train_under, y_pred_train, y_proba_train, "RF")
    test_m = print_detailed_metrics(y_test, y_pred_test, y_proba_test, "RF")
    
    print(f"   Train Acc: {train_m['accuracy']*100:.2f}%")
    print(f"   Test Acc:  {test_m['accuracy']*100:.2f}%")
    print(f"   Overfit:   {(train_m['accuracy']-test_m['accuracy'])*100:+.2f}%")
    print(f"   Precision (bearish/0): {test_m['precision_0']*100:.2f}%")
    print(f"   Precision (bullish/1):  {test_m['precision_1']*100:.2f}%")
    print(f"   Recall (bearish/0):    {test_m['recall_0']*100:.2f}%")
    print(f"   Recall (bullish/1):     {test_m['recall_1']*100:.2f}%")
    print(f"   AUC: {test_m['auc']:.4f}")
    print(f"   Confusion Matrix:\n   {test_m['confusion_matrix']}")
    
    results['random_forest'] = {'train': train_m, 'test': test_m}
    
    # LSTM
    print("\n📈 LSTM + Undersampling:")
    model = train_lstm(X_train_under_3d, y_train_under, X_val, y_val, class_weight=None, bidirectional=False)
    
    y_proba_train = model.predict(X_train_under_3d, verbose=0).flatten()
    y_pred_train = (y_proba_train > 0.5).astype(int)
    y_proba_test = model.predict(X_test, verbose=0).flatten()
    y_pred_test = (y_proba_test > 0.5).astype(int)
    
    train_m = print_detailed_metrics(y_train_under, y_pred_train, y_proba_train, "LSTM")
    test_m = print_detailed_metrics(y_test, y_pred_test, y_proba_test, "LSTM")
    
    print(f"   Train Acc: {train_m['accuracy']*100:.2f}%")
    print(f"   Test Acc:  {test_m['accuracy']*100:.2f}%")
    print(f"   Overfit:   {(train_m['accuracy']-test_m['accuracy'])*100:+.2f}%")
    print(f"   Precision (bearish/0): {test_m['precision_0']*100:.2f}%")
    print(f"   Precision (bullish/1):  {test_m['precision_1']*100:.2f}%")
    print(f"   Recall (bearish/0):    {test_m['recall_0']*100:.2f}%")
    print(f"   Recall (bullish/1):     {test_m['recall_1']*100:.2f}%")
    print(f"   AUC: {test_m['auc']:.4f}")
    print(f"   Confusion Matrix:\n   {test_m['confusion_matrix']}")
    
    results['lstm'] = {'train': train_m, 'test': test_m}
    
    # BiLSTM
    print("\n📈 BiLSTM + Undersampling:")
    model = train_lstm(X_train_under_3d, y_train_under, X_val, y_val, class_weight=None, bidirectional=True)
    
    y_proba_train = model.predict(X_train_under_3d, verbose=0).flatten()
    y_pred_train = (y_proba_train > 0.5).astype(int)
    y_proba_test = model.predict(X_test, verbose=0).flatten()
    y_pred_test = (y_proba_test > 0.5).astype(int)
    
    train_m = print_detailed_metrics(y_train_under, y_pred_train, y_proba_train, "BiLSTM")
    test_m = print_detailed_metrics(y_test, y_pred_test, y_proba_test, "BiLSTM")
    
    print(f"   Train Acc: {train_m['accuracy']*100:.2f}%")
    print(f"   Test Acc:  {test_m['accuracy']*100:.2f}%")
    print(f"   Overfit:   {(train_m['accuracy']-test_m['accuracy'])*100:+.2f}%")
    print(f"   Precision (bearish/0): {test_m['precision_0']*100:.2f}%")
    print(f"   Precision (bullish/1):  {test_m['precision_1']*100:.2f}%")
    print(f"   Recall (bearish/0):    {test_m['recall_0']*100:.2f}%")
    print(f"   Recall (bullish/1):     {test_m['recall_1']*100:.2f}%")
    print(f"   AUC: {test_m['auc']:.4f}")
    print(f"   Confusion Matrix:\n   {test_m['confusion_matrix']}")
    
    results['bilstm'] = {'train': train_m, 'test': test_m}
    
    return results


def print_comparison(baseline, class_weights, smote, undersampling):
    """Print final comparison of all tests"""
    print("\n" + "="*80)
    print("📊 FINAL COMPARISON OF ALL TESTS")
    print("="*80)
    
    models = ['gradient_boost', 'random_forest', 'lstm', 'bilstm']
    model_names = ['Gradient Boost', 'Random Forest', 'LSTM', 'BiLSTM']
    
    print(f"\n{'Model':<20} {'Method':<20} {'Test Acc':<12} {'AUC':<10} {'Recall 0':<12} {'Recall 1':<12} {'Overfit':<10}")
    print("-"*96)
    
    best_overall = {'acc': 0, 'model': '', 'method': ''}
    
    for model, name in zip(models, model_names):
        for method, results in [('Baseline', baseline), ('Class Weights', class_weights), 
                                 ('SMOTE', smote), ('Undersampling', undersampling)]:
            if model in results:
                test = results[model]['test']
                train = results[model]['train']
                overfit = train['accuracy'] - test['accuracy']
                
                # Check if it's the best
                if test['accuracy'] > best_overall['acc'] and overfit < 0.15 and test['recall_0'] > 0.3 and test['recall_1'] > 0.3:
                    best_overall = {'acc': test['accuracy'], 'model': name, 'method': method, 'auc': test['auc']}
                
                status = "✅" if overfit < 0.15 and test['recall_0'] > 0.3 and test['recall_1'] > 0.3 else "⚠️"
                
                print(f"{name:<20} {method:<20} {test['accuracy']*100:>6.2f}%     {test['auc']:>6.4f}   {test['recall_0']*100:>6.2f}%     {test['recall_1']*100:>6.2f}%     {overfit*100:>+6.2f}% {status}")
    
    print("\n" + "="*80)
    print("🏆 BEST OVERALL MODEL:")
    print("="*80)
    if best_overall['acc'] > 0:
        print(f"   Model: {best_overall['model']}")
        print(f"   Method: {best_overall['method']}")
        print(f"   Test Accuracy: {best_overall['acc']*100:.2f}%")
        print(f"   AUC: {best_overall['auc']:.4f}")
        
        if best_overall['acc'] >= 0.52:
            print("\n   ✅ TARGET REACHED! Accuracy >= 52%")
            print("   ✅ Balanced recall (both classes > 30%)")
            print("   ✅ Controlled overfit (< 15%)")
            print("\n   🎉 SUCCESS! Ready for STEP 7!")
        else:
            print("\n   ⚠️ Accuracy below 52%")
            print("   📝 Recommendation: Use ML with low weight (15-20%)")
    else:
        print("   ⚠️ No model met the criteria")
        print("   📝 Recommendation: Use ML with low weight (15%)")


def main():
    print("="*80)
    print("🧪 FINAL TEST: DATASET BALANCING")
    print("="*80)
    print(f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Configuration: seq_length={SEQ_LENGTH}, horizon={HORIZON}h, threshold={THRESHOLD*100}%")
    
    # Load data
    print("\n📂 Loading data...")
    df = load_and_prepare_data()
    print(f"   Total: {len(df):,} rows")
    
    # Prepare features and labels
    feature_columns = get_feature_columns()
    df_labeled = create_labels(df)
    df_clean = df_labeled.dropna(subset=feature_columns + ['label'])
    print(f"   After cleaning: {len(df_clean):,} rows")
    
    # Normalize
    scaler = MinMaxScaler()
    data = scaler.fit_transform(df_clean[feature_columns].values)
    labels = df_clean['label'].values
    
    # Create sequences
    X, y = create_sequences(data, labels)
    print(f"   Sequences: {X.shape}")
    
    # Temporal split (BEFORE any balancing!)
    X_train, X_val, X_test, y_train, y_val, y_test = temporal_split(X, y)
    
    print(f"\n📊 Dataset Distribution:")
    print(f"   Train: {len(y_train):,} samples")
    print(f"      Class 0 (bearish): {(y_train == 0).sum()} ({(y_train == 0).mean()*100:.1f}%)")
    print(f"      Class 1 (bullish):  {(y_train == 1).sum()} ({(y_train == 1).mean()*100:.1f}%)")
    print(f"   Validation: {len(y_val):,} samples")
    print(f"   Test: {len(y_test):,} samples")
    print(f"      Class 0 (bearish): {(y_test == 0).sum()} ({(y_test == 0).mean()*100:.1f}%)")
    print(f"      Class 1 (bullish):  {(y_test == 1).sum()} ({(y_test == 1).mean()*100:.1f}%)")
    
    # Run all tests
    baseline_results = run_baseline_test(X_train, y_train, X_val, y_val, X_test, y_test)
    class_weights_results = run_class_weights_test(X_train, y_train, X_val, y_val, X_test, y_test)
    smote_results = run_smote_test(X_train, y_train, X_val, y_val, X_test, y_test)
    undersampling_results = run_undersampling_test(X_train, y_train, X_val, y_val, X_test, y_test)
    
    # Final comparison
    print_comparison(baseline_results, class_weights_results, smote_results, undersampling_results)
    
    # Save best model
    print("\n💾 Saving configuration...")
    import json
    config = {
        'seq_length': SEQ_LENGTH,
        'horizon': HORIZON,
        'threshold': THRESHOLD,
        'feature_columns': feature_columns,
        'tested_at': datetime.now().isoformat(),
    }
    with open(os.path.join(MODELS_DIR, 'balancing_config.json'), 'w') as f:
        json.dump(config, f, indent=2)
    
    joblib.dump(scaler, os.path.join(MODELS_DIR, 'final_scaler.pkl'))
    print("   ✅ Configuration saved!")
    
    return baseline_results, class_weights_results, smote_results, undersampling_results


if __name__ == "__main__":
    main()
