"""
LSTM Model for XAU/USD Movement Prediction
Project: Trading Bot XAU/USD
Step 6: Machine Learning

This script:
1. Prepares data with features and labels
2. Creates sequences for LSTM
3. Trains LSTM model
4. Evaluates performance
5. Saves model for production
"""

import os
import json
import numpy as np
import pandas as pd
from datetime import datetime
from tz_utils import utc_iso  # FLO-309
import warnings
warnings.filterwarnings('ignore')

# ML Libraries
from sklearn.preprocessing import MinMaxScaler
from sklearn.metrics import (
    accuracy_score, precision_score, recall_score, f1_score,
    roc_auc_score, confusion_matrix, classification_report
)
import joblib

# TensorFlow/Keras
import tensorflow as tf
from tensorflow import keras
from keras.models import Sequential
from keras.layers import LSTM, Dense, Dropout, Input
from keras.callbacks import EarlyStopping, ModelCheckpoint, ReduceLROnPlateau
from keras.optimizers import Adam

# Configuration
DATA_DIR = "data"
MODELS_DIR = "models"
SYMBOL = "XAUUSD"

# Hyperparameters
SEQUENCE_LENGTH = 24  # Lookback window (24 hours) - reduced to capture shorter patterns
PREDICTION_HORIZON = 1  # Predict movement in the next hour
THRESHOLD_UP = 0.0005  # 0.05% to consider "bullish" - more sensitive
TRAIN_RATIO = 0.70
VAL_RATIO = 0.15
TEST_RATIO = 0.15

# Create models directory
os.makedirs(MODELS_DIR, exist_ok=True)


# ============================================================================
# PART 1: DATA PREPARATION
# ============================================================================

def load_data_with_indicators():
    """Load H1 data with already calculated indicators"""
    filepath = os.path.join(DATA_DIR, f"{SYMBOL}_H1_with_indicators.csv")
    
    if not os.path.exists(filepath):
        print(f"❌ File not found: {filepath}")
        print("   Run calculate_indicators.py first!")
        return None
    
    df = pd.read_csv(filepath, parse_dates=['datetime'])
    df = df.sort_values('datetime').reset_index(drop=True)
    
    print(f"✅ Data loaded: {len(df):,} rows")
    print(f"   Period: {df['datetime'].min()} to {df['datetime'].max()}")
    
    return df


def add_extra_features(df):
    """Add extra features that improve performance"""
    
    # Temporal features
    df['hour'] = df['datetime'].dt.hour
    df['day_of_week'] = df['datetime'].dt.dayofweek
    
    # Price changes
    df['price_change_1h'] = df['close'].pct_change(1) * 100
    df['price_change_4h'] = df['close'].pct_change(4) * 100
    df['price_change_24h'] = df['close'].pct_change(24) * 100
    
    # Volatility (range of last N candles)
    df['volatility_4h'] = (df['high'].rolling(4).max() - df['low'].rolling(4).min()) / df['close'] * 100
    df['volatility_24h'] = (df['high'].rolling(24).max() - df['low'].rolling(24).min()) / df['close'] * 100
    
    # Price distance from EMAs (%)
    df['dist_ema9'] = (df['close'] - df['ema_9']) / df['close'] * 100
    df['dist_ema21'] = (df['close'] - df['ema_21']) / df['close'] * 100
    df['dist_ema50'] = (df['close'] - df['ema_50']) / df['close'] * 100
    
    # Position in Bollinger Bands (0-100)
    bb_range = df['bb_upper'] - df['bb_lower']
    df['bb_position'] = np.where(bb_range > 0, 
                                  (df['close'] - df['bb_lower']) / bb_range * 100, 
                                  50)
    
    # MACD momentum (histogram rising or falling)
    df['macd_momentum'] = df['macd_hist'].diff()
    
    # RSI momentum
    df['rsi_momentum'] = df['rsi_14'].diff()
    
    print(f"✅ Extra features added: {len(df.columns)} total columns")
    
    return df


def create_labels(df, horizon=PREDICTION_HORIZON, threshold=THRESHOLD_UP):
    """
    Create labels (target) for binary classification
    Label = 1 if price rose more than threshold in the next N hours
    Label = 0 otherwise
    """
    # Future price (N hours ahead)
    df['future_close'] = df['close'].shift(-horizon)
    
    # Percentage change
    df['future_change'] = (df['future_close'] - df['close']) / df['close']
    
    # Binary label
    df['label'] = (df['future_change'] > threshold).astype(int)
    
    # Remove last rows (no future data)
    df = df.dropna(subset=['label'])
    
    # Statistics
    label_counts = df['label'].value_counts()
    print(f"✅ Labels created:")
    print(f"   Horizon: {horizon} hours")
    print(f"   Threshold: {threshold*100:.2f}%")
    print(f"   Bullish (1): {label_counts.get(1, 0):,} ({label_counts.get(1, 0)/len(df)*100:.1f}%)")
    print(f"   Bearish (0): {label_counts.get(0, 0):,} ({label_counts.get(0, 0)/len(df)*100:.1f}%)")
    
    return df


def select_features(df):
    """Select features for the model"""
    
    # Main features
    feature_columns = [
        # OHLCV
        'open', 'high', 'low', 'close', 'volume',
        
        # Technical indicators
        'ema_9', 'ema_21', 'ema_50',
        'rsi_14',
        'macd', 'macd_signal', 'macd_hist',
        'bb_upper', 'bb_middle', 'bb_lower',
        
        # Extra features
        'hour', 'day_of_week',
        'price_change_1h', 'price_change_4h', 'price_change_24h',
        'volatility_4h', 'volatility_24h',
        'dist_ema9', 'dist_ema21', 'dist_ema50',
        'bb_position',
        'macd_momentum', 'rsi_momentum',
    ]
    
    # Check which ones exist
    available = [col for col in feature_columns if col in df.columns]
    missing = [col for col in feature_columns if col not in df.columns]
    
    if missing:
        print(f"⚠️ Features not found: {missing}")
    
    print(f"✅ Features selected: {len(available)}")
    
    return available


# ============================================================================
# PART 2: NORMALIZATION AND SEQUENCES
# ============================================================================

def normalize_data(df, feature_columns, scaler=None):
    """
    Normalize data using MinMaxScaler
    Returns normalized data and the scaler (for use in production)
    """
    data = df[feature_columns].values
    
    if scaler is None:
        scaler = MinMaxScaler(feature_range=(0, 1))
        normalized = scaler.fit_transform(data)
    else:
        normalized = scaler.transform(data)
    
    return normalized, scaler


def create_sequences(data, labels, sequence_length=SEQUENCE_LENGTH):
    """
    Create sequences for LSTM
    Input: (samples, features)
    Output: X (samples, sequence_length, features), y (samples,)
    """
    X, y = [], []
    
    for i in range(sequence_length, len(data)):
        X.append(data[i-sequence_length:i])
        y.append(labels[i])
    
    X = np.array(X)
    y = np.array(y)
    
    print(f"✅ Sequences created:")
    print(f"   X shape: {X.shape}")
    print(f"   y shape: {y.shape}")
    
    return X, y


# ============================================================================
# PART 3: TEMPORAL SPLIT
# ============================================================================

def temporal_split(X, y, train_ratio=TRAIN_RATIO, val_ratio=VAL_RATIO):
    """
    Split data temporally (NEVER random!)
    """
    n = len(X)
    
    train_end = int(n * train_ratio)
    val_end = int(n * (train_ratio + val_ratio))
    
    X_train = X[:train_end]
    y_train = y[:train_end]
    
    X_val = X[train_end:val_end]
    y_val = y[train_end:val_end]
    
    X_test = X[val_end:]
    y_test = y[val_end:]
    
    print(f"✅ Temporal split:")
    print(f"   Train: {len(X_train):,} samples ({train_ratio*100:.0f}%)")
    print(f"   Validation: {len(X_val):,} samples ({val_ratio*100:.0f}%)")
    print(f"   Test: {len(X_test):,} samples ({(1-train_ratio-val_ratio)*100:.0f}%)")
    
    return X_train, X_val, X_test, y_train, y_val, y_test


# ============================================================================
# PART 4: LSTM MODEL
# ============================================================================

def build_lstm_model(input_shape, units=128):
    """
    Build optimized LSTM model
    
    Architecture:
    - Input Layer
    - LSTM Layer 1 (128 units, return_sequences=True)
    - Dropout (0.3)
    - LSTM Layer 2 (64 units)
    - Dropout (0.3)
    - Dense Layer (32 units, relu)
    - Dense Layer (16 units, relu)
    - Output Layer (1 unit, sigmoid)
    """
    model = Sequential([
        Input(shape=input_shape),
        
        LSTM(units, return_sequences=True, recurrent_dropout=0.1),
        Dropout(0.3),
        
        LSTM(units // 2, return_sequences=False, recurrent_dropout=0.1),
        Dropout(0.3),
        
        Dense(32, activation='relu'),
        Dropout(0.2),
        
        Dense(16, activation='relu'),
        
        Dense(1, activation='sigmoid')
    ])
    
    model.compile(
        optimizer=Adam(learning_rate=0.0005),
        loss='binary_crossentropy',
        metrics=['accuracy', tf.keras.metrics.AUC(name='auc')]
    )
    
    print(f"✅ Model built:")
    model.summary()
    
    return model


# ============================================================================
# PART 5: TRAINING
# ============================================================================

def train_model(model, X_train, y_train, X_val, y_val, epochs=100, batch_size=32):
    """
    Train the model with callbacks and class weights
    """
    # Calculate class weights to handle imbalance
    n_samples = len(y_train)
    n_pos = y_train.sum()
    n_neg = n_samples - n_pos
    
    # Give more weight to the minority class
    weight_for_0 = n_samples / (2 * n_neg) if n_neg > 0 else 1
    weight_for_1 = n_samples / (2 * n_pos) if n_pos > 0 else 1
    class_weight = {0: weight_for_0, 1: weight_for_1}
    
    print(f"   Class weights: 0={weight_for_0:.2f}, 1={weight_for_1:.2f}")
    
    callbacks = [
        EarlyStopping(
            monitor='val_auc',  # Monitor AUC instead of loss
            patience=20,
            restore_best_weights=True,
            mode='max',
            verbose=1
        ),
        ReduceLROnPlateau(
            monitor='val_auc',
            factor=0.5,
            patience=7,
            min_lr=0.00001,
            mode='max',
            verbose=1
        ),
        ModelCheckpoint(
            os.path.join(MODELS_DIR, 'best_model.keras'),
            monitor='val_auc',
            save_best_only=True,
            mode='max',
            verbose=1
        )
    ]
    
    print(f"\n🚀 Starting training...")
    print(f"   Epochs: {epochs}")
    print(f"   Batch size: {batch_size}")
    print(f"   Early stopping patience: 20 (monitoring val_auc)")
    
    history = model.fit(
        X_train, y_train,
        validation_data=(X_val, y_val),
        epochs=epochs,
        batch_size=batch_size,
        callbacks=callbacks,
        class_weight=class_weight,
        verbose=1
    )
    
    return history


# ============================================================================
# PART 6: EVALUATION
# ============================================================================

def evaluate_model(model, X_test, y_test, threshold=0.5):
    """
    Evaluate the model on the test set
    """
    # Predictions
    y_pred_proba = model.predict(X_test, verbose=0)
    y_pred = (y_pred_proba > threshold).astype(int).flatten()
    
    # Metrics
    accuracy = accuracy_score(y_test, y_pred)
    precision = precision_score(y_test, y_pred, zero_division=0)
    recall = recall_score(y_test, y_pred, zero_division=0)
    f1 = f1_score(y_test, y_pred, zero_division=0)
    
    try:
        auc = roc_auc_score(y_test, y_pred_proba)
    except:
        auc = 0.5
    
    # Confusion Matrix
    cm = confusion_matrix(y_test, y_pred)
    
    print(f"\n{'='*60}")
    print(f"📊 TEST SET METRICS")
    print(f"{'='*60}")
    print(f"Accuracy:  {accuracy*100:.2f}% {'✅' if accuracy > 0.55 else '⚠️'}")
    print(f"Precision: {precision*100:.2f}%")
    print(f"Recall:    {recall*100:.2f}%")
    print(f"F1-Score:  {f1:.4f}")
    print(f"AUC-ROC:   {auc:.4f} {'✅' if auc > 0.55 else '⚠️'}")
    
    print(f"\n📋 Confusion Matrix:")
    print(f"              Predicted")
    print(f"              0     1")
    print(f"Actual 0   {cm[0,0]:5d} {cm[0,1]:5d}")
    print(f"       1   {cm[1,0]:5d} {cm[1,1]:5d}")
    
    # Baseline comparison
    baseline = max(y_test.mean(), 1 - y_test.mean())
    print(f"\n📈 Baseline Comparison:")
    print(f"   Baseline (always majority): {baseline*100:.2f}%")
    print(f"   Model:                      {accuracy*100:.2f}%")
    print(f"   Gain:                       {(accuracy-baseline)*100:+.2f}%")
    
    return {
        'accuracy': accuracy,
        'precision': precision,
        'recall': recall,
        'f1': f1,
        'auc': auc,
        'confusion_matrix': cm,
        'y_pred': y_pred,
        'y_pred_proba': y_pred_proba,
    }


def backtest_trades(df_test, y_pred_proba, y_test, threshold=0.6):
    """
    Simulate trades using model predictions
    """
    print(f"\n{'='*60}")
    print(f"💰 TRADE BACKTEST")
    print(f"{'='*60}")
    
    trades = []
    
    for i in range(len(y_pred_proba)):
        prob = y_pred_proba[i][0]
        
        if prob > threshold:  # Buy signal
            actual_label = y_test[i] if i < len(y_test) else 0
            win = actual_label == 1
            trades.append({
                'prob': prob,
                'actual_label': actual_label,
                'win': win
            })
    
    if not trades:
        print(f"   No trades with confidence > {threshold*100:.0f}%")
        return {}
    
    trades_df = pd.DataFrame(trades)
    
    total_trades = len(trades_df)
    wins = trades_df['win'].sum()
    win_rate = wins / total_trades if total_trades > 0 else 0
    
    print(f"   Confidence threshold: {threshold*100:.0f}%")
    print(f"   Total trades: {total_trades}")
    print(f"   Wins: {wins} | Losses: {total_trades - wins}")
    print(f"   Win Rate: {win_rate*100:.1f}% {'✅' if win_rate > 0.55 else '⚠️'}")
    
    return {
        'total_trades': total_trades,
        'win_rate': win_rate,
    }


# ============================================================================
# PART 7: SAVE MODEL
# ============================================================================

def save_model_artifacts(model, scaler, feature_columns, metrics):
    """
    Save all artifacts needed for production
    """
    print(f"\n{'='*60}")
    print(f"💾 SAVING ARTIFACTS")
    print(f"{'='*60}")
    
    # Model
    model_path = os.path.join(MODELS_DIR, 'lstm_model.keras')
    model.save(model_path)
    print(f"   ✅ Model: {model_path}")
    
    # Scaler
    scaler_path = os.path.join(MODELS_DIR, 'scaler.pkl')
    joblib.dump(scaler, scaler_path)
    print(f"   ✅ Scaler: {scaler_path}")
    
    # Feature columns
    features_path = os.path.join(MODELS_DIR, 'feature_columns.json')
    with open(features_path, 'w') as f:
        json.dump(feature_columns, f, indent=2)
    print(f"   ✅ Features: {features_path}")
    
    # Hyperparameters and metrics
    config = {
        'sequence_length': SEQUENCE_LENGTH,
        'prediction_horizon': PREDICTION_HORIZON,
        'threshold_up': THRESHOLD_UP,
        'train_ratio': TRAIN_RATIO,
        'val_ratio': VAL_RATIO,
        'test_ratio': TEST_RATIO,
        'n_features': len(feature_columns),
        'metrics': {
            'accuracy': float(metrics['accuracy']),
            'precision': float(metrics['precision']),
            'recall': float(metrics['recall']),
            'f1': float(metrics['f1']),
            'auc': float(metrics['auc']),
        },
        'trained_at': utc_iso(),  # FLO-309: was datetime.now() = local
    }
    
    config_path = os.path.join(MODELS_DIR, 'model_config.json')
    with open(config_path, 'w') as f:
        json.dump(config, f, indent=2)
    print(f"   ✅ Config: {config_path}")


# ============================================================================
# PART 8: PREDICTION FUNCTION FOR PRODUCTION
# ============================================================================

def load_model_for_prediction():
    """
    Load model and artifacts for making predictions
    """
    model_path = os.path.join(MODELS_DIR, 'lstm_model.keras')
    scaler_path = os.path.join(MODELS_DIR, 'scaler.pkl')
    features_path = os.path.join(MODELS_DIR, 'feature_columns.json')
    config_path = os.path.join(MODELS_DIR, 'model_config.json')
    
    model = keras.models.load_model(model_path)
    scaler = joblib.load(scaler_path)
    
    with open(features_path, 'r') as f:
        feature_columns = json.load(f)
    
    with open(config_path, 'r') as f:
        config = json.load(f)
    
    return model, scaler, feature_columns, config


def predict_next_movement(df_recent, model=None, scaler=None, feature_columns=None):
    """
    Make prediction for the next hours
    
    Args:
        df_recent: DataFrame with last SEQUENCE_LENGTH candles (with indicators)
        model, scaler, feature_columns: Artifacts (loads if not provided)
    
    Returns:
        dict with probability and score 0-100
    """
    # Load artifacts if not provided
    if model is None:
        model, scaler, feature_columns, _ = load_model_for_prediction()
    
    # Check if there is enough data
    if len(df_recent) < SEQUENCE_LENGTH:
        return {
            'error': f'Insufficient data. Need {SEQUENCE_LENGTH} candles, have {len(df_recent)}',
            'score': 50,
        }
    
    # Get last SEQUENCE_LENGTH candles
    df_last = df_recent.tail(SEQUENCE_LENGTH)
    
    # Select features
    try:
        data = df_last[feature_columns].values
    except KeyError as e:
        return {
            'error': f'Feature not found: {e}',
            'score': 50,
        }
    
    # Normalize
    data_normalized = scaler.transform(data)
    
    # Reshape for LSTM: (1, sequence_length, n_features)
    X = data_normalized.reshape(1, SEQUENCE_LENGTH, len(feature_columns))
    
    # Prediction
    prob = model.predict(X, verbose=0)[0][0]
    
    # Convert to score 0-100
    score = round(prob * 100, 1)
    
    # Interpretation
    if score >= 70:
        interpretation = "🟢 STRONG BULLISH"
    elif score >= 60:
        interpretation = "🟢 BULLISH"
    elif score >= 55:
        interpretation = "🟡 SLIGHTLY BULLISH"
    elif score <= 30:
        interpretation = "🔴 STRONG BEARISH"
    elif score <= 40:
        interpretation = "🔴 BEARISH"
    elif score <= 45:
        interpretation = "🟡 SLIGHTLY BEARISH"
    else:
        interpretation = "⚪ NEUTRAL"
    
    return {
        'probability': float(prob),
        'score': score,
        'interpretation': interpretation,
        'horizon_hours': PREDICTION_HORIZON,
    }


# ============================================================================
# MAIN - COMPLETE PIPELINE
# ============================================================================

def main():
    print("=" * 70)
    print("🧠 LSTM MODEL - XAU/USD PRICE PREDICTION")
    print("=" * 70)
    print(f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print()
    
    # 1. Load data
    print("\n" + "─" * 70)
    print("📂 PART 1: LOADING DATA")
    print("─" * 70)
    
    df = load_data_with_indicators()
    if df is None:
        return
    
    # 2. Add extra features
    print("\n" + "─" * 70)
    print("🔧 PART 2: PREPARING FEATURES")
    print("─" * 70)
    
    df = add_extra_features(df)
    
    # 3. Create labels
    df = create_labels(df)
    
    # 4. Select features
    feature_columns = select_features(df)
    
    # 5. Remove NaN
    df_clean = df.dropna(subset=feature_columns + ['label'])
    print(f"✅ Clean data: {len(df_clean):,} rows (removed {len(df) - len(df_clean):,} NaN)")
    
    # 6. Normalize
    print("\n" + "─" * 70)
    print("📊 PART 3: NORMALIZING AND CREATING SEQUENCES")
    print("─" * 70)
    
    data_normalized, scaler = normalize_data(df_clean, feature_columns)
    labels = df_clean['label'].values
    
    # 7. Create sequences
    X, y = create_sequences(data_normalized, labels)
    
    # 8. Temporal split
    print("\n" + "─" * 70)
    print("✂️ PART 4: TEMPORAL SPLIT")
    print("─" * 70)
    
    X_train, X_val, X_test, y_train, y_val, y_test = temporal_split(X, y)
    
    # Store indices for backtest
    test_start_idx = int(len(df_clean) * (TRAIN_RATIO + VAL_RATIO)) + SEQUENCE_LENGTH
    df_test = df_clean.iloc[test_start_idx:test_start_idx + len(y_test)].reset_index(drop=True)
    
    # 9. Build model
    print("\n" + "─" * 70)
    print("🏗️ PART 5: BUILDING LSTM MODEL")
    print("─" * 70)
    
    input_shape = (X_train.shape[1], X_train.shape[2])
    model = build_lstm_model(input_shape)
    
    # 10. Train
    print("\n" + "─" * 70)
    print("🚀 PART 6: TRAINING")
    print("─" * 70)
    
    history = train_model(model, X_train, y_train, X_val, y_val, epochs=100, batch_size=32)
    
    # 11. Evaluate
    print("\n" + "─" * 70)
    print("📈 PART 7: EVALUATION")
    print("─" * 70)
    
    metrics = evaluate_model(model, X_test, y_test)
    
    # 12. Backtest
    backtest_results = backtest_trades(df_test, metrics['y_pred_proba'], y_test, threshold=0.5)
    
    # 13. Save
    print("\n" + "─" * 70)
    print("💾 PART 8: SAVING MODEL")
    print("─" * 70)
    
    save_model_artifacts(model, scaler, feature_columns, metrics)
    
    # 14. Final summary
    print("\n" + "=" * 70)
    print("📊 FINAL SUMMARY")
    print("=" * 70)
    
    print(f"""
📈 DATASET:
   Train: {len(X_train):,} samples ({TRAIN_RATIO*100:.0f}%)
   Validation: {len(X_val):,} samples ({VAL_RATIO*100:.0f}%)
   Test: {len(X_test):,} samples ({TEST_RATIO*100:.0f}%)
   Sequence Length: {SEQUENCE_LENGTH} candles
   Features: {len(feature_columns)}

🎯 TEST SET METRICS:
   Accuracy:  {metrics['accuracy']*100:.2f}% {'✅' if metrics['accuracy'] > 0.55 else '⚠️'}
   Precision: {metrics['precision']*100:.2f}%
   Recall:    {metrics['recall']*100:.2f}%
   F1-Score:  {metrics['f1']:.4f}
   AUC-ROC:   {metrics['auc']:.4f}

📉 TRAINING:
   Final epochs: {len(history.history['loss'])}
   Final train loss: {history.history['loss'][-1]:.4f}
   Final val loss: {history.history['val_loss'][-1]:.4f}
""")
    
    if backtest_results:
        print(f"""💰 BACKTEST:
   Total Trades: {backtest_results.get('total_trades', 0)}
   Win Rate: {backtest_results.get('win_rate', 0)*100:.1f}%
""")
    
    status = "✅ MODEL READY FOR PRODUCTION!" if metrics['accuracy'] > 0.55 else "⚠️ Model needs improvement"
    print(status)
    print("=" * 70)
    
    return model, scaler, feature_columns, metrics


if __name__ == "__main__":
    main()
