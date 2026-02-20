"""
Experiments to Improve the LSTM Model
Tests different configurations and compares results
"""

import os
import numpy as np
import pandas as pd
from datetime import datetime
import warnings
warnings.filterwarnings('ignore')

# ML Libraries
from sklearn.preprocessing import MinMaxScaler
from sklearn.metrics import accuracy_score, precision_score, recall_score, f1_score, roc_auc_score
from sklearn.ensemble import RandomForestClassifier, GradientBoostingClassifier
from sklearn.utils.class_weight import compute_class_weight
import joblib

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
    """List of features for the model"""
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


def create_labels(df, horizon, threshold):
    """Create labels for classification"""
    df = df.copy()
    df['future_close'] = df['close'].shift(-horizon)
    df['future_change'] = (df['future_close'] - df['close']) / df['close']
    df['label'] = (df['future_change'] > threshold).astype(int)
    return df.dropna(subset=['label'])


def create_sequences(data, labels, seq_length):
    """Create sequences for LSTM"""
    X, y = [], []
    for i in range(seq_length, len(data)):
        X.append(data[i-seq_length:i])
        y.append(labels[i])
    return np.array(X), np.array(y)


def temporal_split(X, y, train_ratio=0.70, val_ratio=0.15):
    """Temporal split of data"""
    n = len(X)
    train_end = int(n * train_ratio)
    val_end = int(n * (train_ratio + val_ratio))
    
    return (
        X[:train_end], X[train_end:val_end], X[val_end:],
        y[:train_end], y[train_end:val_end], y[val_end:]
    )


def build_lstm_model(input_shape, units=128, bidirectional=False):
    """Build LSTM model"""
    model = Sequential()
    model.add(Input(shape=input_shape))
    
    if bidirectional:
        model.add(Bidirectional(LSTM(units, return_sequences=True)))
    else:
        model.add(LSTM(units, return_sequences=True, recurrent_dropout=0.1))
    model.add(Dropout(0.3))
    
    if bidirectional:
        model.add(Bidirectional(LSTM(units // 2)))
    else:
        model.add(LSTM(units // 2, recurrent_dropout=0.1))
    model.add(Dropout(0.3))
    
    model.add(Dense(32, activation='relu'))
    model.add(Dropout(0.2))
    model.add(Dense(16, activation='relu'))
    model.add(Dense(1, activation='sigmoid'))
    
    model.compile(
        optimizer=Adam(learning_rate=0.0005),
        loss='binary_crossentropy',
        metrics=['accuracy', tf.keras.metrics.AUC(name='auc')]
    )
    return model


def train_lstm(X_train, y_train, X_val, y_val, units=128, bidirectional=False, epochs=50):
    """Train LSTM model"""
    # Class weights
    n_pos = y_train.sum()
    n_neg = len(y_train) - n_pos
    class_weight = {
        0: len(y_train) / (2 * n_neg) if n_neg > 0 else 1,
        1: len(y_train) / (2 * n_pos) if n_pos > 0 else 1
    }
    
    model = build_lstm_model((X_train.shape[1], X_train.shape[2]), units, bidirectional)
    
    callbacks = [
        EarlyStopping(monitor='val_auc', patience=10, restore_best_weights=True, mode='max'),
        ReduceLROnPlateau(monitor='val_auc', factor=0.5, patience=5, min_lr=0.00001, mode='max')
    ]
    
    history = model.fit(
        X_train, y_train,
        validation_data=(X_val, y_val),
        epochs=epochs,
        batch_size=32,
        callbacks=callbacks,
        class_weight=class_weight,
        verbose=0
    )
    
    return model, history


def train_random_forest(X_train, y_train):
    """Train Random Forest"""
    # Flatten sequences for RF
    X_train_flat = X_train.reshape(X_train.shape[0], -1)
    
    # Class weights
    classes = np.unique(y_train)
    weights = compute_class_weight('balanced', classes=classes, y=y_train)
    class_weight = dict(zip(classes, weights))
    
    model = RandomForestClassifier(
        n_estimators=200,
        max_depth=10,
        min_samples_split=10,
        class_weight=class_weight,
        random_state=42,
        n_jobs=-1
    )
    model.fit(X_train_flat, y_train)
    return model


def train_gradient_boosting(X_train, y_train):
    """Train Gradient Boosting (XGBoost-like)"""
    X_train_flat = X_train.reshape(X_train.shape[0], -1)
    
    model = GradientBoostingClassifier(
        n_estimators=100,
        max_depth=5,
        learning_rate=0.1,
        subsample=0.8,
        random_state=42
    )
    model.fit(X_train_flat, y_train)
    return model


def evaluate_model(model, X_test, y_test, is_lstm=True):
    """Evaluate model and return metrics"""
    if is_lstm:
        y_pred_proba = model.predict(X_test, verbose=0).flatten()
    else:
        X_test_flat = X_test.reshape(X_test.shape[0], -1)
        y_pred_proba = model.predict_proba(X_test_flat)[:, 1]
    
    y_pred = (y_pred_proba > 0.5).astype(int)
    
    return {
        'accuracy': accuracy_score(y_test, y_pred),
        'precision': precision_score(y_test, y_pred, zero_division=0),
        'recall': recall_score(y_test, y_pred, zero_division=0),
        'f1': f1_score(y_test, y_pred, zero_division=0),
        'auc': roc_auc_score(y_test, y_pred_proba) if len(np.unique(y_test)) > 1 else 0.5,
    }


def run_experiment(df, seq_length, horizon, threshold, model_type='lstm', bidirectional=False):
    """Run a complete experiment"""
    feature_columns = get_feature_columns()
    
    # Prepare data
    df_labeled = create_labels(df, horizon, threshold)
    df_clean = df_labeled.dropna(subset=feature_columns + ['label'])
    
    # Normalize
    scaler = MinMaxScaler()
    data = scaler.fit_transform(df_clean[feature_columns].values)
    labels = df_clean['label'].values
    
    # Create sequences
    X, y = create_sequences(data, labels, seq_length)
    
    # Split
    X_train, X_val, X_test, y_train, y_val, y_test = temporal_split(X, y)
    
    # Balance info
    train_balance = y_train.mean()
    test_balance = y_test.mean()
    
    # Train
    if model_type == 'lstm':
        model, history = train_lstm(X_train, y_train, X_val, y_val, bidirectional=bidirectional)
        train_metrics = evaluate_model(model, X_train, y_train, is_lstm=True)
    elif model_type == 'rf':
        model = train_random_forest(X_train, y_train)
        train_metrics = evaluate_model(model, X_train, y_train, is_lstm=False)
    elif model_type == 'gb':
        model = train_gradient_boosting(X_train, y_train)
        train_metrics = evaluate_model(model, X_train, y_train, is_lstm=False)
    
    # Evaluate
    test_metrics = evaluate_model(model, X_test, y_test, is_lstm=(model_type == 'lstm'))
    
    return {
        'model': model,
        'scaler': scaler,
        'train_metrics': train_metrics,
        'test_metrics': test_metrics,
        'train_balance': train_balance,
        'test_balance': test_balance,
        'n_train': len(y_train),
        'n_test': len(y_test),
    }


def main():
    print("=" * 80)
    print("🧪 ML EXPERIMENTS - XAU/USD")
    print("=" * 80)
    print(f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
    
    # Load data
    print("📂 Loading data...")
    df = load_and_prepare_data()
    print(f"   Total: {len(df):,} rows\n")
    
    # Experiments to test
    experiments = [
        # (seq_length, horizon, threshold, model_type, bidirectional, name)
        (60, 4, 0.001, 'lstm', False, "LSTM seq=60, h=4h, th=0.1%"),
        (60, 4, 0.002, 'lstm', False, "LSTM seq=60, h=4h, th=0.2%"),
        (60, 8, 0.003, 'lstm', False, "LSTM seq=60, h=8h, th=0.3%"),
        (48, 4, 0.001, 'lstm', True, "BiLSTM seq=48, h=4h, th=0.1%"),
        (60, 4, 0.001, 'rf', False, "RandomForest seq=60, h=4h"),
        (60, 4, 0.001, 'gb', False, "GradientBoost seq=60, h=4h"),
    ]
    
    results = []
    best_accuracy = 0
    best_experiment = None
    best_model = None
    best_scaler = None
    
    for seq_len, horizon, threshold, model_type, bidir, name in experiments:
        print(f"\n{'─'*80}")
        print(f"🔬 Experiment: {name}")
        print(f"{'─'*80}")
        
        try:
            result = run_experiment(df, seq_len, horizon, threshold, model_type, bidir)
            
            train_acc = result['train_metrics']['accuracy']
            test_acc = result['test_metrics']['accuracy']
            test_auc = result['test_metrics']['auc']
            
            # Check overfitting
            overfit = train_acc - test_acc
            overfit_status = "⚠️ OVERFIT" if overfit > 0.15 else ("✅" if overfit < 0.10 else "🟡")
            
            print(f"   📊 Train Balance: {result['train_balance']*100:.1f}% positive")
            print(f"   📊 Test Balance:  {result['test_balance']*100:.1f}% positive")
            print(f"   📈 Train Accuracy: {train_acc*100:.2f}%")
            print(f"   📈 Test Accuracy:  {test_acc*100:.2f}%")
            print(f"   📈 Test AUC:       {test_auc:.4f}")
            print(f"   📈 Test Precision: {result['test_metrics']['precision']*100:.2f}%")
            print(f"   📈 Test Recall:    {result['test_metrics']['recall']*100:.2f}%")
            print(f"   📈 Test F1:        {result['test_metrics']['f1']:.4f}")
            print(f"   🔍 Overfit Gap:    {overfit*100:+.2f}% {overfit_status}")
            
            results.append({
                'name': name,
                'seq_len': seq_len,
                'horizon': horizon,
                'threshold': threshold,
                'model_type': model_type,
                'train_acc': train_acc,
                'test_acc': test_acc,
                'test_auc': test_auc,
                'overfit': overfit,
            })
            
            # Best model?
            if test_acc > best_accuracy and overfit < 0.15:
                best_accuracy = test_acc
                best_experiment = name
                best_model = result['model']
                best_scaler = result['scaler']
                best_config = {
                    'seq_length': seq_len,
                    'horizon': horizon,
                    'threshold': threshold,
                    'model_type': model_type,
                }
                
        except Exception as e:
            print(f"   ❌ Error: {e}")
    
    # Summary
    print("\n" + "=" * 80)
    print("📊 EXPERIMENT SUMMARY")
    print("=" * 80)
    
    print(f"\n{'Experiment':<40} {'Train':<10} {'Test':<10} {'AUC':<10} {'Overfit':<10}")
    print("-" * 80)
    
    for r in sorted(results, key=lambda x: x['test_acc'], reverse=True):
        overfit_str = f"{r['overfit']*100:+.1f}%"
        status = "⚠️" if r['overfit'] > 0.15 else "✅"
        print(f"{r['name']:<40} {r['train_acc']*100:>6.2f}%   {r['test_acc']*100:>6.2f}%   {r['test_auc']:>6.4f}   {overfit_str:>6} {status}")
    
    # Best model
    print(f"\n{'='*80}")
    print(f"🏆 BEST MODEL: {best_experiment}")
    print(f"   Test Accuracy: {best_accuracy*100:.2f}%")
    print(f"{'='*80}")
    
    # Save best model
    if best_model is not None and best_accuracy > 0.50:
        print("\n💾 Saving best model...")
        
        if best_config['model_type'] == 'lstm':
            best_model.save(os.path.join(MODELS_DIR, 'best_lstm_model.keras'))
        else:
            joblib.dump(best_model, os.path.join(MODELS_DIR, f"best_{best_config['model_type']}_model.pkl"))
        
        joblib.dump(best_scaler, os.path.join(MODELS_DIR, 'best_scaler.pkl'))
        
        import json
        with open(os.path.join(MODELS_DIR, 'best_config.json'), 'w') as f:
            json.dump({
                **best_config,
                'accuracy': best_accuracy,
                'feature_columns': get_feature_columns(),
            }, f, indent=2)
        
        print("   ✅ Model saved!")
    
    return results


if __name__ == "__main__":
    main()
