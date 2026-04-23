"""
ML PREDICTOR - Prediction with Ensemble v3
Uses 6 models: XGBoost + LightGBM + CatBoost × H1 + H4 horizons.
Features include technical + macro + session + multi-timeframe (H4, M5) +
sentiment proxy + regime detection + feature interactions.
Rank-based calibration per model. Blend: 0.4*H1 + 0.6*H4.
"""

import os
import json
import numpy as np
import pandas as pd
from typing import Dict, Tuple, Optional
import config
from logger import log


class MLPredictor:
    """ML Predictor using Ensemble (XGB+LGB+CAT × H1+H4)"""
    
    def __init__(self):
        self.models = {}       # key: 'xgb_h1', 'lgb_h4', etc.
        self.features = {}     # key: 'xgb_h1' → list of feature names
        self.percentiles = {}  # key: 'xgb_h1' → dict of p10/p50/p90
        self.weights = {}      # key: 'h1' → {'xgb': 0.335, ...}
        self.horizon_blend = {'h1': 0.4, 'h4': 0.6}
        self.ensemble_config = None
        self.loaded = False
        self._models_dir = config.MODELS_DIR if hasattr(config, 'MODELS_DIR') else 'models'
    
    def load_model(self) -> bool:
        """Load ensemble config and all 6 models"""
        try:
            config_path = os.path.join(self._models_dir, 'ensemble_config.json')
            
            if not os.path.exists(config_path):
                print(f"⚠️ Ensemble config not found: {config_path}")
                return self._load_fallback_xgboost()
            
            with open(config_path) as f:
                self.ensemble_config = json.load(f)
            
            self.weights = self.ensemble_config.get('ensemble_weights', {})
            self.horizon_blend = self.ensemble_config.get('horizon_blend', {'h1': 0.4, 'h4': 0.6})
            
            loaded_count = 0
            for model_key, model_info in self.ensemble_config.get('models', {}).items():
                algo = model_key.split('_')[0]  # 'xgb', 'lgb', 'cat'
                model_file = model_info['model_file']
                model_path = os.path.join(self._models_dir, model_file)
                
                if not os.path.exists(model_path):
                    print(f"  ⚠️ Model not found: {model_file}")
                    continue
                
                model = self._load_single_model(algo, model_path)
                if model is not None:
                    self.models[model_key] = model
                    self.features[model_key] = model_info['features']
                    self.percentiles[model_key] = model_info.get('probability_percentiles', {})
                    loaded_count += 1
            
            if loaded_count == 0:
                print("⚠️ No ensemble models loaded")
                return False
            
            self.loaded = True
            print(f"✅ Ensemble loaded: {loaded_count}/6 models ({', '.join(self.models.keys())})")
            return True
            
        except Exception as e:
            print(f"Error loading ensemble: {e}")
            return False
    
    def _load_single_model(self, algo: str, path: str):
        """Load a single model by algorithm type."""
        try:
            if algo == 'xgb':
                import xgboost as xgb
                model = xgb.XGBClassifier()
                model.load_model(path)
                return model
            elif algo == 'lgb':
                import lightgbm as lgb
                model = lgb.Booster(model_file=path)
                return model
            elif algo == 'cat':
                import catboost as cb
                model = cb.CatBoostClassifier()
                model.load_model(path)
                return model
        except Exception as e:
            print(f"  ⚠️ Failed to load {algo} from {path}: {e}")
        return None
    
    def _load_fallback_xgboost(self) -> bool:
        """Fallback: load single XGBoost v2 if ensemble not available."""
        try:
            import xgboost as xgb
            model_path = os.path.join(self._models_dir, 'xgboost_model.json')
            features_path = os.path.join(self._models_dir, 'xgboost_features.json')
            config_path = os.path.join(self._models_dir, 'xgboost_config.json')
            
            if not os.path.exists(model_path):
                return False

            if not os.path.exists(features_path):
                log.warning(f"Fallback XGB features not found: {features_path}")
                return False
            if not os.path.exists(config_path):
                log.warning(f"Fallback XGB config not found: {config_path}")
                return False

            model = xgb.XGBClassifier()
            model.load_model(model_path)

            with open(features_path) as f:
                feats = json.load(f)
            with open(config_path) as f:
                cfg = json.load(f)
            
            self.models['xgb_h1'] = model
            self.features['xgb_h1'] = feats
            self.percentiles['xgb_h1'] = cfg.get('probability_percentiles', {})
            self.weights = {'h1': {'xgb': 1.0}}
            self.horizon_blend = {'h1': 1.0}
            self.loaded = True
            print(f"✅ Fallback XGBoost v2 loaded: {len(feats)} features")
            return True
        except Exception as e:
            print(f"Error fallback XGBoost: {e}")
            return False
    
    def prepare_features(self, df: pd.DataFrame,
                         news_data: Optional[Dict] = None) -> Optional[pd.Series]:
        """
        Prepare all features for the ensemble.
        Returns the last row as pd.Series (each model selects its own).
        """
        df = df.copy()
        
        try:
            # === TECHNICAL FEATURES ===
            if 'datetime' in df.columns:
                dt = pd.to_datetime(df['datetime'])
                df['hour'] = dt.dt.hour
                df['day_of_week'] = dt.dt.dayofweek
            else:
                df['hour'] = 12
                df['day_of_week'] = 2
            
            df['price_change_1h'] = df['close'].pct_change(1) * 100
            df['price_change_4h'] = df['close'].pct_change(4) * 100
            df['price_change_24h'] = df['close'].pct_change(24) * 100
            
            df['volatility_4h'] = (df['high'].rolling(4).max() - df['low'].rolling(4).min()) / df['close'] * 100
            df['volatility_24h'] = (df['high'].rolling(24).max() - df['low'].rolling(24).min()) / df['close'] * 100
            
            if 'ema_9' in df.columns:
                df['dist_ema9'] = (df['close'] - df['ema_9']) / df['close'] * 100
            else:
                df['dist_ema9'] = 0.0
            
            if 'ema_21' in df.columns:
                df['dist_ema21'] = (df['close'] - df['ema_21']) / df['close'] * 100
            else:
                df['dist_ema21'] = 0.0
            
            if 'ema_50' in df.columns:
                df['dist_ema50'] = (df['close'] - df['ema_50']) / df['close'] * 100
            else:
                df['dist_ema50'] = 0.0
            
            # Bollinger position
            if 'bb_upper' in df.columns and 'bb_lower' in df.columns:
                bb_range = df['bb_upper'] - df['bb_lower']
                df['bb_position'] = np.where(bb_range > 0, (df['close'] - df['bb_lower']) / bb_range * 100, 50)
            else:
                df['bb_position'] = 50.0
            
            if 'macd_hist' in df.columns:
                df['macd_momentum'] = df['macd_hist'].diff()
            else:
                df['macd_momentum'] = 0.0
            
            if 'rsi_14' in df.columns:
                df['rsi_momentum'] = df['rsi_14'].diff()
            else:
                df['rsi_14'] = 50.0
                df['rsi_momentum'] = 0.0
            
            # Lagged returns
            df['gold_return_lag1'] = df['close'].pct_change(1).shift(1) * 100
            df['gold_return_lag4'] = df['close'].pct_change(4).shift(1) * 100
            
            # === SESSION FEATURES ===
            df['session'] = df['hour'].apply(lambda h: 0 if 0 <= h < 8 else (1 if 8 <= h < 14 else (2 if 14 <= h < 21 else 3)))
            df['is_london_open'] = df['hour'].apply(lambda h: 1 if h in (7, 8) else 0)
            df['is_ny_open'] = df['hour'].apply(lambda h: 1 if h in (13, 14) else 0)
            
            # === MULTI-TIMEFRAME FEATURES ===
            df['price_change_1W'] = df['close'].pct_change(120) * 100
            
            # price_vs_ema50_D1 (approximate from H1 EMA50 * ~24 = D1 EMA50)
            if 'ema_50' in df.columns:
                df['price_vs_ema50_D1'] = (df['close'] - df['ema_50']) / df['close'] * 100
            else:
                df['price_vs_ema50_D1'] = 0.0
            
            # ATR ratio
            if 'atr' in df.columns:
                atr_d1_approx = df['close'].diff().abs().rolling(24 * 14).mean()
                df['atr_ratio_H1_vs_D1'] = df['atr'] / atr_d1_approx.replace(0, np.nan)
            else:
                df['atr_ratio_H1_vs_D1'] = 0.18
            
            # === MACRO FEATURES (from news_data) ===
            dxy_change = 0.0
            vix_level = 17.0
            vix_change = 0.0
            yields_change = 0.0
            news_score_real = 50.0
            
            if news_data:
                dxy_info = news_data.get('dxy', {})
                if dxy_info.get('change_24h') is not None:
                    dxy_change = float(dxy_info['change_24h'])
                
                vix_info = news_data.get('vix', {})
                if vix_info.get('value') is not None:
                    vix_level = float(vix_info['value'])
                if vix_info.get('change') is not None:
                    vix_change = float(vix_info['change'])
                elif vix_info.get('change_24h') is not None:
                    vix_change = float(vix_info['change_24h'])

                yields_info = news_data.get('yields', {})
                if yields_info.get('change_24h') is not None:
                    yields_change = float(yields_info['change_24h'])
                
                news_score_real = float(news_data.get('score') or 50.0)
            
            df['dxy_change_1d'] = dxy_change
            df['vix_level'] = vix_level
            df['vix_change'] = vix_change
            df['yields_10y_change'] = yields_change
            df['sp500_change_1d'] = 0.0
            df['dxy_level'] = 0.0
            df['dxy_change_lag1'] = dxy_change
            df['vix_change_lag1'] = vix_change
            
            # === CROSS-ASSET FEATURES ===
            xag_change = self._get_xag_change_1h()
            df['xag_change_1h'] = xag_change
            df['xag_change_4h'] = xag_change * 2  # approximation
            df['xag_xau_ratio'] = 0.0
            df['oil_change_1d'] = 0.0
            
            # === H4 FEATURES (from MT5 live) ===
            h4_feats = self._get_h4_features()
            df['rsi_H4'] = h4_feats['rsi_H4']
            df['price_change_H4'] = h4_feats['price_change_H4']
            df['dist_ema21_H4'] = h4_feats['dist_ema21_H4']
            
            # === M5 FEATURES (from MT5 live) ===
            m5_feats = self._get_m5_features()
            df['volume_spike_M5'] = m5_feats['volume_spike_M5']
            df['price_change_M30'] = m5_feats.get('price_change_M30', 0.0)
            df['price_change_M15'] = 0.0  # Alias of momentum_M15 — kept for model compatibility until retrain
            df['momentum_M15'] = 0.0
            df['consecutive_candles_M15'] = 0.0
            df['price_vs_vwap_intraday'] = 0.0
            
            # === SENTIMENT PROXY (Group 9) ===
            # In training: formula from DXY+VIX+Yields
            # In live: use real news_score from GPT (same 0-100 range)
            sentiment_proxy_formula = float(np.clip(
                50 - dxy_change * 10 + yields_change * 5 - vix_change * 2, 0, 100))
            df['sentiment_proxy'] = news_score_real
            
            # Log proxy vs real for monitoring
            log.debug(f"ML sentiment: proxy={sentiment_proxy_formula:.1f} "
                      f"real_news={news_score_real:.1f} "
                      f"delta={abs(sentiment_proxy_formula - news_score_real):.1f}")
            
            # === REGIME DETECTION ===
            abs_ema50_d1 = abs(df['price_vs_ema50_D1'].iloc[-1]) if 'price_vs_ema50_D1' in df.columns else 0
            atr_r = df['atr_ratio_H1_vs_D1'].iloc[-1] if 'atr_ratio_H1_vs_D1' in df.columns else 0.18
            if pd.isna(atr_r):
                atr_r = 0.18
            if pd.isna(abs_ema50_d1):
                abs_ema50_d1 = 0.5
            regime = 0  # ranging
            if abs_ema50_d1 > 2.0:
                regime = 1  # trending
            if vix_level > 25 or atr_r > 0.25:
                regime = 2  # volatile
            df['regime'] = regime
            
            # === FEATURE INTERACTIONS ===
            df['dxy_x_vix'] = dxy_change * vix_level
            df['momentum_x_volume'] = df['price_change_H4'] * df['volume_spike_M5']
            df['trend_x_session'] = df['dist_ema21_H4'] * df['is_ny_open']
            
            # Fill NaN
            for col in df.columns:
                df[col] = df[col].fillna(0.0)
            
            # Return last row as Series
            return df.iloc[-1]
            
        except Exception as e:
            print(f"Error preparing ML features: {e}")
            return None
    
    def _get_xag_change_1h(self) -> float:
        """Fetch XAGUSD 1h change from MT5."""
        try:
            from mt5_safe import mt5  # FLO-348
            if not mt5.terminal_info():
                return 0.0
            
            rates = mt5.copy_rates_from_pos("XAGUSD", mt5.TIMEFRAME_H1, 0, 2)
            if rates is not None and len(rates) >= 2:
                current = rates[-1]['close']
                previous = rates[-2]['close']
                if previous > 0:
                    return ((current - previous) / previous) * 100
            return 0.0
        except Exception:
            return 0.0
    
    def _get_m5_features(self) -> Dict:
        """Fetch M5 microstructure features from MT5 live (last N M5 candles)."""
        defaults = {'volume_spike_M5': 0.0, 'price_change_M30': 0.0}
        try:
            from mt5_safe import mt5  # FLO-348
            if not mt5.terminal_info():
                return defaults
            
            rates = mt5.copy_rates_from_pos("XAUUSD", mt5.TIMEFRAME_M5, 0, 25)
            if rates is None or len(rates) < 20:
                return defaults
            
            m5 = pd.DataFrame(rates)
            
            # volume_spike_M5: volume of last 3 / avg of last 20
            vol_last3 = m5['tick_volume'].iloc[-3:].sum()
            vol_avg20 = m5['tick_volume'].iloc[-20:].mean()
            vol_spike = (vol_last3 / (3 * vol_avg20)) if vol_avg20 > 0 else 0.0
            
            # price_change_M30: % change over last 6 M5 candles (30 min)
            price_change_m30 = 0.0
            if len(m5) >= 7:
                c_now = float(m5['close'].iloc[-1])
                c_6ago = float(m5['close'].iloc[-7])
                if c_6ago > 0:
                    price_change_m30 = ((c_now - c_6ago) / c_6ago) * 100
            
            return {
                'volume_spike_M5': float(vol_spike),
                'price_change_M30': float(price_change_m30),
            }
        except Exception:
            return defaults
    
    def _get_h4_features(self) -> Dict:
        """Fetch H4 features from MT5 live."""
        defaults = {'rsi_H4': 50.0, 'price_change_H4': 0.0, 'dist_ema21_H4': 0.0}
        try:
            from mt5_safe import mt5  # FLO-348
            if not mt5.terminal_info():
                return defaults
            
            rates = mt5.copy_rates_from_pos("XAUUSD", mt5.TIMEFRAME_H4, 0, 30)
            if rates is None or len(rates) < 22:
                return defaults
            
            h4 = pd.DataFrame(rates)
            
            # price_change_H4: 1-bar return %
            price_change = (h4['close'].iloc[-1] / h4['close'].iloc[-2] - 1) * 100
            
            # RSI H4 (manual calculation, 14-period)
            delta = h4['close'].diff()
            gain = delta.where(delta > 0, 0.0).rolling(14).mean()
            loss = (-delta.where(delta < 0, 0.0)).rolling(14).mean()
            rs = gain / loss.replace(0, np.nan)
            rsi = 100 - (100 / (1 + rs))
            rsi_val = float(rsi.iloc[-1]) if not np.isnan(rsi.iloc[-1]) else 50.0
            
            # dist_ema21_H4
            ema21 = h4['close'].ewm(span=21, adjust=False).mean()
            dist = (h4['close'].iloc[-1] - ema21.iloc[-1]) / h4['close'].iloc[-1] * 100
            
            return {
                'rsi_H4': rsi_val,
                'price_change_H4': float(price_change),
                'dist_ema21_H4': float(dist),
            }
        except Exception:
            return defaults
    
    def _calibrate_score(self, raw_proba: float, pcts: Dict) -> float:
        """
        Rank-based calibration for a single model.
        P10→25, P50→50, P90→75, clamp [10, 90].
        """
        p10 = pcts.get('p10', 0.07)
        p50 = pcts.get('p50', 0.24)
        p90 = pcts.get('p90', 0.56)
        
        if raw_proba <= p10:
            score = 25.0 * (raw_proba / max(p10, 1e-6))
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
    
    def _predict_single(self, model_key: str, row: pd.Series) -> Optional[float]:
        """Get raw probability from a single model."""
        model = self.models.get(model_key)
        feats = self.features.get(model_key)
        if model is None or feats is None:
            return None
        
        try:
            X = row[feats].values.reshape(1, -1).astype(np.float64)
            algo = model_key.split('_')[0]
            
            if algo == 'lgb':
                proba = model.predict(X)[0]
                proba = 1.0 / (1.0 + np.exp(-proba))  # sigmoid: raw margin → probability
            else:
                proba = model.predict_proba(X)[0][1]
            
            return float(proba)
        except Exception as e:
            log.debug(f"ML predict failed for {model_key}: {e}")
            return None
    
    def predict(self, df: pd.DataFrame,
                news_data: Optional[Dict] = None) -> Dict:
        """
        Make prediction with ensemble (6 models, 2 horizons).
        
        Returns:
            Dict with score, score_h1, score_h4, direction, etc.
        """
        if not self.models:
            if not self.load_model():
                return {
                    'prediction': 0, 'probability': 0.5, 'score': 50.0,
                    'direction': 'NEUTRAL', 'error': 'Ensemble not loaded'
                }
        
        row = self.prepare_features(df, news_data)
        if row is None:
            return {
                'prediction': 0, 'probability': 0.5, 'score': 50.0,
                'direction': 'NEUTRAL', 'error': 'Error preparing features'
            }
        
        try:
            # Predict with each model, calibrate, blend per horizon
            horizon_scores = {}
            model_details = {}
            
            for horizon in ['h1', 'h4']:
                w = self.weights.get(horizon, {})
                weighted_score = 0.0
                total_weight = 0.0
                
                for algo in ['xgb', 'lgb', 'cat']:
                    key = f'{algo}_{horizon}'
                    raw = self._predict_single(key, row)
                    if raw is None:
                        continue
                    
                    pcts = self.percentiles.get(key, {})
                    cal_score = self._calibrate_score(raw, pcts)
                    algo_weight = w.get(algo, 1.0 / 3)
                    
                    weighted_score += cal_score * algo_weight
                    total_weight += algo_weight
                    model_details[key] = {'raw': raw, 'score': cal_score}
                
                if total_weight > 0:
                    horizon_scores[horizon] = weighted_score / total_weight
                else:
                    horizon_scores[horizon] = 50.0

            # P0-2: Check if any model actually produced a prediction
            if not model_details:
                return {
                    'prediction': 0, 'probability': 0.5, 'score': 50.0,
                    'direction': 'NEUTRAL', 'error': 'all_models_failed'
                }

            # Blend H1 + H4
            h1_w = self.horizon_blend.get('h1', 0.4)
            h4_w = self.horizon_blend.get('h4', 0.6)
            score_h1 = horizon_scores.get('h1', 50.0)
            score_h4 = horizon_scores.get('h4', 50.0)
            
            final_score = score_h1 * h1_w + score_h4 * h4_w
            final_score = max(10.0, min(90.0, final_score))
            
            # Direction
            if final_score >= 55:
                direction = 'BUY'
                prediction = 1
            elif final_score <= 45:
                direction = 'SELL'
                prediction = 0
            else:
                direction = 'NEUTRAL'
                prediction = 0
            
            # Max confidence
            deviation = abs(final_score - 50.0) / 40.0
            max_confidence = 0.5 + min(1.0, deviation) * 0.5
            
            # Log ensemble details
            detail_str = " | ".join(f"{k}={v['score']:.1f}" for k, v in sorted(model_details.items()))
            log.debug(f"ML ensemble: H1={score_h1:.1f} H4={score_h4:.1f} → {final_score:.1f} [{detail_str}]")
            
            return {
                'prediction': int(prediction),
                'probability': float(final_score / 100),
                'score': float(final_score),
                'score_h1': float(score_h1),
                'score_h4': float(score_h4),
                'direction': direction,
                'max_confidence': float(max_confidence),
                'raw_proba': float(final_score / 100),
                'model_details': model_details,
                'error': None
            }
            
        except Exception as e:
            return {
                'prediction': 0, 'probability': 0.5, 'score': 50.0,
                'direction': 'NEUTRAL', 'error': str(e)
            }


# Global instance
predictor = MLPredictor()

# Cache for news_data (set by main.py before calling get_ml_detailed)
_cached_news_data: Optional[Dict] = None


def set_news_data_for_ml(news_data: Dict):
    """Cache news_data for ML features. Called from main.py before get_ml_detailed."""
    global _cached_news_data
    _cached_news_data = news_data


# ============================================================================
# CONVENIENCE FUNCTIONS
# ============================================================================

def predict_next_movement(df: pd.DataFrame) -> Dict:
    """Make prediction of next movement"""
    return predictor.predict(df, _cached_news_data)


def get_ml_score(df: pd.DataFrame) -> Tuple[float, float]:
    """
    Return ML score and probability.
    
    Returns:
        Tuple: (score, probability)
    """
    result = predictor.predict(df, _cached_news_data)
    return result['score'], result.get('raw_proba', result['probability'])


# ============================================================================
# DETAILED ANALYSIS (for the Central Brain)
# ============================================================================

def get_ml_detailed(df: pd.DataFrame, news_data: Optional[Dict] = None) -> Dict:
    """
    Return detailed ML data for the Central Brain.
    
    Includes prediction, probability, inferred pattern and score.
    Inferred pattern based on:
        - ML agrees with trend (price vs EMA9) = "continuacao"
        - ML contradicts trend = "reversao"
        - Medium confidence (60-65%) = "breakout"
        - Low confidence (<=60%) = "indefinido"
    
    Args:
        df: DataFrame with calculated indicators
        news_data: Dict from get_news_detailed() (optional, uses cache if None)
    
    Returns:
        Dict with all detailed ML data
    """
    # Use provided news_data or cached
    effective_news = news_data or _cached_news_data
    
    # Get base prediction
    result = predictor.predict(df, effective_news)
    
    score = result['score']
    direction = result['direction']
    prediction = result['prediction']
    
    # Determine direction as bullish/bearish
    if direction == 'BUY':
        ml_direction = "bullish"
    elif direction == 'SELL':
        ml_direction = "bearish"
    else:
        ml_direction = "neutral"
    
    # Maximum confidence
    max_confidence = result.get('max_confidence', 0.5)
    
    # Infer pattern based on table D3
    pattern = "indefinido"
    if df is not None and len(df) > 0 and 'ema_9' in df.columns:
        current_price = float(df['close'].iloc[-1])
        ema9 = float(df['ema_9'].iloc[-1])
        price_above_ema9 = current_price > ema9
        
        if max_confidence > 0.65:
            if ml_direction == "bullish":
                pattern = "continuacao" if price_above_ema9 else "reversao"
            elif ml_direction == "bearish":
                pattern = "continuacao" if not price_above_ema9 else "reversao"
        elif max_confidence > 0.60:
            pattern = "breakout"
        else:
            pattern = "indefinido"
    
    return {
        "score": float(score),
        "score_h1": float(result.get('score_h1', score)),
        "score_h4": float(result.get('score_h4', score)),
        "prediction": ml_direction,
        "probability": float(result.get('raw_proba', result['probability'])),
        "max_confidence": float(max_confidence),
        "pattern": pattern,
        "similar_patterns_count": None,
        "historical_success_rate": None,
        "error": result.get('error'),
    }


# ============================================================================
# TEST
# ============================================================================

def test_ml_predictor():
    """Test the ML predictor (Ensemble v3)"""
    print("=" * 60)
    print("🧪 ML PREDICTOR TEST (Ensemble v3)")
    print("=" * 60)
    
    # Create simulated data
    np.random.seed(42)
    n = 200
    
    base_price = 2650
    prices = base_price + np.cumsum(np.random.randn(n) * 2)
    
    df = pd.DataFrame({
        'datetime': pd.date_range(end=pd.Timestamp.now(), periods=n, freq='h'),
        'open': prices - np.random.rand(n),
        'high': prices + np.random.rand(n) * 3,
        'low': prices - np.random.rand(n) * 3,
        'close': prices,
        'volume': np.random.randint(1000, 5000, n)
    })
    
    # Calculate basic indicators
    df['ema_9'] = df['close'].ewm(span=9, adjust=False).mean()
    df['ema_21'] = df['close'].ewm(span=21, adjust=False).mean()
    df['ema_50'] = df['close'].ewm(span=50, adjust=False).mean()
    df['rsi_14'] = 50.0 + np.random.randn(n) * 10
    
    ema_12 = df['close'].ewm(span=12, adjust=False).mean()
    ema_26 = df['close'].ewm(span=26, adjust=False).mean()
    df['macd'] = ema_12 - ema_26
    df['macd_signal'] = df['macd'].ewm(span=9, adjust=False).mean()
    df['macd_hist'] = df['macd'] - df['macd_signal']
    
    df['bb_upper'] = df['close'].rolling(20).mean() + df['close'].rolling(20).std() * 2
    df['bb_lower'] = df['close'].rolling(20).mean() - df['close'].rolling(20).std() * 2
    
    print(f"\n📊 Simulated data: {len(df)} bars")
    
    # Simulate news_data
    fake_news = {
        'score': 45.0,
        'dxy': {'value': 104.5, 'change_24h': -0.3, 'trend': 'caindo'},
        'yields': {'value': 4.2, 'change_24h': 0.5, 'trend': 'subindo'},
        'vix': {'value': 18.5, 'level': 'baixo'},
    }
    
    # Test prediction
    print("\n🤖 Testing Ensemble prediction...")
    result = predictor.predict(df, fake_news)
    
    print(f"\n📈 Result:")
    print(f"   Direction: {result['direction']}")
    print(f"   Final score: {result['score']:.1f}/100")
    print(f"   H1 score: {result.get('score_h1', 'N/A')}")
    print(f"   H4 score: {result.get('score_h4', 'N/A')}")
    print(f"   Max confidence: {result.get('max_confidence', 'N/A')}")
    print(f"   Models loaded: {len(predictor.models)}")
    
    if result.get('model_details'):
        print(f"   Model details:")
        for k, v in sorted(result['model_details'].items()):
            print(f"     {k}: raw={v['raw']:.4f} score={v['score']:.1f}")
    
    if result['error']:
        print(f"   ⚠️ Error: {result['error']}")
    
    # Test get_ml_detailed
    print("\n🧠 Testing get_ml_detailed...")
    detailed = get_ml_detailed(df, fake_news)
    print(f"   Score: {detailed['score']:.1f}/100")
    print(f"   Prediction: {detailed['prediction']}")
    print(f"   Max confidence: {detailed['max_confidence']:.2f}")
    print(f"   Pattern: {detailed['pattern']}")


if __name__ == "__main__":
    test_ml_predictor()
