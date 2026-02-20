"""
Retrain XGBoost Model — XAUUSD Trading Bot
============================================
Script for periodic retraining of the XGBoost model.
Runs the full pipeline: collect → train → save.

Usage:
    python scripts/retrain_model.py              # Full retrain
    python scripts/retrain_model.py --skip-collect  # Train only (data already exists)

Recommendation: run monthly or when accuracy drops.
"""

import sys
import os
import argparse
from datetime import datetime

# Add parent dir to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main():
    parser = argparse.ArgumentParser(description='Retrain XGBoost model')
    parser.add_argument('--skip-collect', action='store_true',
                        help='Skip data collection (use existing dataset)')
    parser.add_argument('--trials', type=int, default=100,
                        help='Number of Optuna trials (default: 100)')
    args = parser.parse_args()

    print("=" * 70)
    print("🔄 RETRAIN XGBOOST MODEL — XAUUSD")
    print("=" * 70)
    print(f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Skip collect: {args.skip_collect}")
    print(f"Optuna trials: {args.trials}")
    print()

    # --- Step 1: Collect data ---
    if not args.skip_collect:
        print("📦 Step 1: Collecting training data...")
        print("-" * 70)
        from collect_training_data import main as collect_main
        collect_main()
        print()
    else:
        print("⏭️ Step 1: Skipped (--skip-collect)")
        print()

    # --- Step 2: Train model ---
    print("🧠 Step 2: Training XGBoost model...")
    print("-" * 70)

    # Override n_trials if specified
    from train_xgboost import main as train_main
    import train_xgboost
    original_main = train_xgboost.main

    # Patch n_trials in the training script
    if args.trials != 100:
        original_run_optuna = train_xgboost.run_optuna_tuning
        def patched_run_optuna(df, features, labels, n_trials=100):
            return original_run_optuna(df, features, labels, n_trials=args.trials)
        train_xgboost.run_optuna_tuning = patched_run_optuna

    train_main()

    print(f"\n{'='*70}")
    print("✅ RETRAIN COMPLETE!")
    print(f"{'='*70}")
    print(f"Finished: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("Restart the bot to use the new model.")


if __name__ == "__main__":
    main()
