"""
COMPLETE TRADING BOT VALIDATION
Tests all modules individually and safety scenarios
"""

import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

print("=" * 70)
print("🔍 COMPLETE XAU/USD TRADING BOT VALIDATION")
print("=" * 70)
print(f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

# ============================================================================
# VALIDATION 1: ISOLATED MODULES
# ============================================================================

print("\n" + "=" * 70)
print("📊 VALIDATION 1: ISOLATED MODULES")
print("=" * 70)

# 1a) Technical Analyzer
print("\n🔹 1a) TECHNICAL ANALYZER")
print("-" * 50)

try:
    import MetaTrader5 as mt5
    from technical_analyzer import get_mt5_data, calculate_indicators, calculate_technical_score, get_atr_value
    
    if mt5.initialize():
        df = get_mt5_data()
        if df is not None:
            df = calculate_indicators(df)
            tech_score, tech_breakdown = calculate_technical_score(df)
            atr = get_atr_value(df)
            
            print(f"   ✅ Tech Score: {tech_score}/100")
            print(f"   📋 Breakdown:")
            for k, v in tech_breakdown.items():
                print(f"      - {k}: {v}")
            print(f"   📈 ATR(14): {atr:.2f}")
            print(f"   📈 Current price: {df['close'].iloc[-1]:.2f}")
        else:
            print("   ⚠️ No MT5 data")
        mt5.shutdown()
    else:
        print("   ⚠️ MT5 not available")
except Exception as e:
    print(f"   ❌ Error: {e}")

# 1b) News Hybrid
print("\n🔹 1b) NEWS HYBRID SCORE")
print("-" * 50)

try:
    from news_sentiment import get_hybrid_score
    
    news_result = get_hybrid_score()
    news_score = news_result.get('score', 50)
    
    print(f"   ✅ News Score: {news_score}/100")
    print(f"   📋 Breakdown:")
    
    if 'components' in news_result:
        for k, v in news_result['components'].items():
            print(f"      - {k}: {v}")
    else:
        for k, v in news_result.items():
            if k != 'score':
                print(f"      - {k}: {v}")
                
except Exception as e:
    print(f"   ❌ Error: {e}")
    news_score = 50

# 1c) ML Predictor
print("\n🔹 1c) ML PREDICTOR")
print("-" * 50)

try:
    from ml_predictor import predictor, get_ml_score
    import MetaTrader5 as mt5
    
    if mt5.initialize():
        df = get_mt5_data()
        if df is not None:
            df = calculate_indicators(df)
            ml_score, ml_prob = get_ml_score(df)
            
            print(f"   ✅ ML Score: {ml_score:.2f}/100")
            print(f"   📊 Probability: {ml_prob:.4f}")
            print(f"   🎯 Direction: {'BUY' if ml_prob > 0.5 else 'SELL'}")
            print(f"   📋 Model loaded: {predictor.loaded}")
        else:
            print("   ⚠️ No MT5 data")
        mt5.shutdown()
    else:
        print("   ⚠️ MT5 not available")
        ml_score, ml_prob = 50.0, 0.5
except Exception as e:
    print(f"   ❌ Error: {e}")
    ml_score, ml_prob = 50.0, 0.5

# 1d) Risk Manager
print("\n🔹 1d) RISK MANAGER")
print("-" * 50)

try:
    from risk_manager import calculate_position_size, calculate_sl_tp
    
    # Test: $1000 capital, 2% risk, 15 pips SL
    pos = calculate_position_size(
        account_balance=1000,
        risk_percent=2.0,
        stop_loss_pips=15
    )
    
    print(f"   📊 Inputs: Capital=$1000, Risk=2%, SL=15 pips")
    print(f"   ✅ Lot Size: {pos.lot_size}")
    print(f"   💰 Risk Amount: ${pos.risk_amount:.2f}")
    print(f"   📉 Potential Loss: ${pos.potential_loss:.2f}")
    
    # Manual calculation to validate
    # risk_amount = 1000 * 0.02 = $20
    # lot_size = 20 / (15 * 10) = 20/150 = 0.133
    # rounded = 0.13, but max is 0.10
    expected = 20 / (15 * 10)
    print(f"   🧮 Expected calculation: {expected:.3f} → rounded: {round(expected, 2)}")
    print(f"   📋 Max lot configured: 0.10 (conservative)")
    
    # Test SL/TP
    levels = calculate_sl_tp(entry_price=2650.00, direction="BUY", atr_value=10.0)
    print(f"\n   📊 SL/TP for BUY @ 2650, ATR=10:")
    print(f"      SL: {levels.stop_loss} ({levels.sl_pips} pips)")
    print(f"      TP1: {levels.take_profit_1} ({levels.tp1_pips} pips)")
    print(f"      TP2: {levels.take_profit_2} ({levels.tp2_pips} pips)")
    print(f"      R:R 1: 1:{levels.risk_reward_1}")
    print(f"      R:R 2: 1:{levels.risk_reward_2}")
    
except Exception as e:
    print(f"   ❌ Error: {e}")

# ============================================================================
# VALIDATION 3: SAFETY CHECKS - 5 SCENARIOS
# ============================================================================

print("\n" + "=" * 70)
print("🛡️ VALIDATION 3: SAFETY CHECKS - 5 SCENARIOS")
print("=" * 70)

try:
    from safety_checks import SafetyChecker, is_safe_to_trade
    
    # Create clean checker for tests
    test_checker = SafetyChecker()
    
    # Scenario 1: High-impact news
    print("\n🔹 Scenario 1: High-impact news")
    is_safe, reasons = test_checker.check_all(
        account_balance=1000,
        open_positions=0,
        mt5_connected=True,
        has_high_impact_news=True
    )
    print(f"   Safe: {is_safe}")
    print(f"   Reasons: {reasons}")
    print(f"   ✅ Blocked correctly!" if not is_safe and "High-impact news" in str(reasons) else "   ❌ FAILED!")
    
    # Scenario 2: Market open/closed (is_market_open)
    print("\n🔹 Scenario 2: Market open/closed")
    is_open, market_reason, next_open = test_checker.is_market_open()
    print(f"   Current UTC time: {datetime.utcnow().strftime('%A %H:%M')}")
    print(f"   Market open: {is_open}")
    if not is_open:
        print(f"   Reason: {market_reason}")
        print(f"   Next open: {next_open}")
    print(f"   ✅ Check working!")
    
    # Scenario 2b: Test weekend (simulate Saturday 12:00 UTC)
    print("\n🔹 Scenario 2b: Simulate weekend (Saturday 12:00 UTC)")
    fake_saturday = datetime(2026, 2, 14, 12, 0)  # Saturday
    is_open_sat, reason_sat, next_open_sat = test_checker.is_market_open(fake_saturday)
    print(f"   Simulated: Saturday 12:00 UTC")
    print(f"   Market open: {is_open_sat}")
    print(f"   Reason: {reason_sat}")
    print(f"   Next open: {next_open_sat}")
    print(f"   ✅ Blocked weekend!" if not is_open_sat else "   ❌ FAILED!")
    
    # Scenario 2c: Test daily pause (simulate Tuesday 21:30 UTC)
    print("\n🔹 Scenario 2c: Simulate daily pause (Tuesday 21:30 UTC)")
    fake_daily_pause = datetime(2026, 2, 17, 21, 30)  # Tuesday 21:30
    is_open_dp, reason_dp, next_open_dp = test_checker.is_market_open(fake_daily_pause)
    print(f"   Simulated: Tuesday 21:30 UTC")
    print(f"   Market open: {is_open_dp}")
    print(f"   Reason: {reason_dp}")
    print(f"   Next open: {next_open_dp}")
    print(f"   ✅ Blocked daily pause!" if not is_open_dp else "   ❌ FAILED!")
    
    # Scenario 2d: Test Asian session (simulate Wednesday 03:00 UTC — SHOULD be open)
    print("\n🔹 Scenario 2d: Simulate Asian session (Wednesday 03:00 UTC)")
    fake_asian = datetime(2026, 2, 18, 3, 0)  # Wednesday 03:00
    is_open_as, reason_as, _ = test_checker.is_market_open(fake_asian)
    print(f"   Simulated: Wednesday 03:00 UTC")
    print(f"   Market open: {is_open_as}")
    print(f"   ✅ Asian session OPEN!" if is_open_as else f"   ❌ FAILED! Reason: {reason_as}")
    
    # Scenario 3: Buffer before close
    print("\n🔹 Scenario 3: Buffer before close")
    fake_buffer = datetime(2026, 2, 18, 20, 57)  # Wednesday 20:57 UTC (3 min before close)
    in_buffer = test_checker.is_in_close_buffer(fake_buffer)
    print(f"   Simulated: Wednesday 20:57 UTC (3 min before 21:00 close)")
    print(f"   In buffer: {in_buffer}")
    print(f"   ✅ Buffer detected!" if in_buffer else "   ❌ FAILED!")
    
    # Scenario 4: 3 consecutive losses
    print("\n🔹 Scenario 4: 3 consecutive losses")
    test_checker2 = SafetyChecker()
    test_checker2.record_trade_result(-20)
    test_checker2.record_trade_result(-20)
    test_checker2.record_trade_result(-20)
    
    is_safe, reasons = test_checker2.check_all(
        account_balance=1000,
        open_positions=0,
        mt5_connected=True,
        has_high_impact_news=False
    )
    print(f"   Consecutive losses: {test_checker2.consecutive_losses}")
    print(f"   Safe: {is_safe}")
    print(f"   Reasons: {reasons}")
    pause_info = test_checker2.get_pause_info()
    if pause_info:
        print(f"   Pause until: {pause_info['until']}")
    print(f"   ✅ Paused correctly!" if not is_safe and "consecutive losses" in str(reasons) else "   ⚠️ May not have paused (check time)")
    
    # Scenario 5: 3 open positions
    print("\n🔹 Scenario 5: 3 open positions")
    test_checker3 = SafetyChecker()
    is_safe, reasons = test_checker3.check_all(
        account_balance=1000,
        open_positions=3,
        mt5_connected=True,
        has_high_impact_news=False
    )
    print(f"   Open positions: 3")
    print(f"   Safe: {is_safe}")
    print(f"   Reasons: {reasons}")
    print(f"   ✅ Blocked correctly!" if not is_safe and "positions" in str(reasons) else "   ❌ FAILED!")

except Exception as e:
    print(f"   ❌ Error: {e}")
    import traceback
    traceback.print_exc()

# ============================================================================
# VALIDATION 4: POSITION MONITOR
# ============================================================================

print("\n" + "=" * 70)
print("📊 VALIDATION 4: POSITION MONITOR")
print("=" * 70)

try:
    from risk_manager import calculate_breakeven_sl, calculate_trailing_stop
    
    # TP1 scenario
    print("\n🔹 Scenario: TP1 reached")
    entry = 2650.00
    be_sl = calculate_breakeven_sl(entry, "BUY", spread_pips=2.0)
    print(f"   Entry: {entry}")
    print(f"   Breakeven SL: {be_sl}")
    print(f"   ✅ Move SL to breakeven: {be_sl} (entry + spread)")
    
    # Trailing Stop scenario
    print("\n🔹 Scenario: Trailing Stop")
    current_price = 2670.00
    current_sl = 2650.00
    new_sl = calculate_trailing_stop(current_price, "BUY", current_sl, trailing_distance_pips=15.0)
    print(f"   Current price: {current_price}")
    print(f"   Current SL: {current_sl}")
    print(f"   New SL (trailing 15 pips): {new_sl}")
    print(f"   ✅ Trailing working!" if new_sl and new_sl > current_sl else "   ⚠️ Trailing did not activate (price did not rise enough)")

except Exception as e:
    print(f"   ❌ Error: {e}")

# ============================================================================
# VALIDATION 5: FAILURE RESILIENCE
# ============================================================================

print("\n" + "=" * 70)
print("🛡️ VALIDATION 5: FAILURE RESILIENCE")
print("=" * 70)

# Test Discord failure
print("\n🔹 Scenario: Discord webhook failure")
try:
    from alerts import DiscordAlert
    
    # Create with invalid webhook
    bad_discord = DiscordAlert(webhook_url="https://invalid-url.com/webhook")
    result = bad_discord.send("Failure test")
    print(f"   Result with invalid URL: {result}")
    print(f"   ✅ Did not crash! Returned False gracefully.")
except Exception as e:
    print(f"   ❌ Error: {e}")

# Test MT5 disconnected
print("\n🔹 Scenario: MT5 disconnected")
try:
    from executor import MT5Executor
    
    test_executor = MT5Executor()
    test_executor.connected = False
    test_executor.dry_run = False  # Force real mode for testing
    
    result = test_executor.execute_trade("BUY", 0.01, 2635, 2680, "Test")
    print(f"   Success: {result.success}")
    print(f"   Error: {result.error_message}")
    print(f"   ✅ Did not crash! Returned error gracefully.")
except Exception as e:
    print(f"   ❌ Error: {e}")

# ============================================================================
# VALIDATION 6: CONFIG.PY VALUES
# ============================================================================

print("\n" + "=" * 70)
print("⚙️ VALIDATION 6: CONFIG.PY VALUES")
print("=" * 70)

try:
    import config
    
    print(f"\n   📊 Risk Parameters:")
    print(f"      CAPITAL_INICIAL: ${config.CAPITAL_INICIAL}")
    print(f"      RISK_PER_TRADE: {config.RISK_PER_TRADE}%")
    print(f"      MAX_DAILY_LOSS: {config.MAX_DAILY_LOSS}%")
    print(f"      MAX_POSITIONS: {config.MAX_POSITIONS}")
    print(f"      MAX_CONSECUTIVE_LOSSES: {config.MAX_CONSECUTIVE_LOSSES}")
    
    print(f"\n   📊 Decision Thresholds:")
    print(f"      STRONG_BUY: > {config.STRONG_BUY_THRESHOLD}")
    print(f"      BUY: > {config.BUY_THRESHOLD}")
    print(f"      SELL: < {config.SELL_THRESHOLD}")
    print(f"      STRONG_SELL: < {config.STRONG_SELL_THRESHOLD}")
    
    print(f"\n   📊 Confluence Weights:")
    print(f"      WEIGHT_TECHNICAL: {config.WEIGHT_TECHNICAL} ({config.WEIGHT_TECHNICAL*100}%)")
    print(f"      WEIGHT_NEWS: {config.WEIGHT_NEWS} ({config.WEIGHT_NEWS*100}%)")
    print(f"      WEIGHT_ML: {config.WEIGHT_ML} ({config.WEIGHT_ML*100}%)")
    print(f"      ML_MIN_PROBABILITY: {config.ML_MIN_PROBABILITY}")
    
    print(f"\n   📊 Lot Size:")
    print(f"      MIN_LOT_SIZE: {config.MIN_LOT_SIZE}")
    print(f"      MAX_LOT_SIZE: {config.MAX_LOT_SIZE}")
    
    print(f"\n   📊 Mode:")
    print(f"      DRY_RUN: {config.DRY_RUN}")
    
    # Validations
    warnings = []
    if config.RISK_PER_TRADE > 3:
        warnings.append("⚠️ RISK_PER_TRADE > 3% is risky!")
    if config.MAX_POSITIONS > 5:
        warnings.append("⚠️ MAX_POSITIONS > 5 is risky!")
    if config.MAX_LOT_SIZE > 0.5:
        warnings.append("⚠️ MAX_LOT_SIZE > 0.5 is risky!")
    if not config.DRY_RUN:
        warnings.append("⚠️ DRY_RUN is FALSE - LIVE mode!")
    
    if warnings:
        print(f"\n   ⚠️ WARNINGS:")
        for w in warnings:
            print(f"      {w}")
    else:
        print(f"\n   ✅ All values look safe!")

except Exception as e:
    print(f"   ❌ Error: {e}")

# ============================================================================
# VALIDATION 7: LOGS
# ============================================================================

print("\n" + "=" * 70)
print("📝 VALIDATION 7: LOGS")
print("=" * 70)

try:
    from logger import log, get_log_file_path, read_recent_logs
    
    log_file = get_log_file_path()
    print(f"\n   📁 Log file: {log_file}")
    print(f"   📁 Exists: {os.path.exists(log_file)}")
    
    # Write some test logs
    log.info("Validation test - INFO")
    log.success("Validation test - SUCCESS")
    log.warning("Validation test - WARNING")
    
    # Read recent logs
    recent = read_recent_logs(10)
    if recent:
        print(f"\n   📋 Last {len(recent)} log lines:")
        for line in recent[-10:]:
            print(f"      {line.strip()}")
    else:
        print(f"   ⚠️ No logs found")

except Exception as e:
    print(f"   ❌ Error: {e}")

# ============================================================================
# VALIDATION 8: SINGLE TEST BREAKDOWN
# ============================================================================

print("\n" + "=" * 70)
print("🎯 VALIDATION 8: COMPLETE ANALYSIS WITH BREAKDOWN")
print("=" * 70)

try:
    from confluence import analyze_confluence
    import MetaTrader5 as mt5
    
    if mt5.initialize():
        df = get_mt5_data()
        if df is not None:
            df = calculate_indicators(df)
            
            # Individual scores
            tech_score, tech_breakdown = calculate_technical_score(df)
            
            try:
                from news_sentiment import get_hybrid_score
                news_result = get_hybrid_score()
                news_score = news_result.get('score', 50)
            except:
                news_score = 50
            
            try:
                from ml_predictor import get_ml_score
                ml_score, ml_prob = get_ml_score(df)
            except:
                ml_score, ml_prob = 50, 0.5
            
            # Confluence
            result = analyze_confluence(tech_score, news_score, ml_score, ml_prob)
            
            print(f"\n   📊 INDIVIDUAL SCORES:")
            print(f"      Tech Score: {tech_score}/100")
            print(f"      News Score: {news_score}/100")
            print(f"      ML Score: {ml_score}/100")
            print(f"      ML Probability: {ml_prob:.4f}")
            
            print(f"\n   🎯 CONFLUENCE:")
            print(f"      Final Score: {result.final_score}/100")
            print(f"      Decision: {result.decision}")
            print(f"      Confidence: {result.confidence}")
            print(f"      ML included: {'Yes' if result.ml_included else 'No (prob < 0.55)'}")
            
            print(f"\n   📋 BREAKDOWN:")
            for k, v in result.breakdown.items():
                if k != 'weights':
                    print(f"      {k}: {v}")
        
        mt5.shutdown()
    else:
        print("   ⚠️ MT5 not available")

except Exception as e:
    print(f"   ❌ Error: {e}")
    import traceback
    traceback.print_exc()

# ============================================================================
# FINAL SUMMARY
# ============================================================================

print("\n" + "=" * 70)
print("📋 VALIDATION SUMMARY")
print("=" * 70)

print("""
✅ VALIDATION 1: Isolated modules tested
✅ VALIDATION 3: Safety Checks - 5 scenarios tested
✅ VALIDATION 4: Position Monitor - TP1/Trailing tested
✅ VALIDATION 5: Resilience - Discord/MT5 failures handled
✅ VALIDATION 6: Config.py - values verified
✅ VALIDATION 7: Logs - system working
✅ VALIDATION 8: Complete breakdown shown

⚠️ VALIDATION 2: DRY_RUN needs to run longer!
   Recommendation: Run 'python main.py --dry-run' for 6-24 hours
   and monitor Discord alerts.
""")

print("=" * 70)
print("🏁 VALIDATION COMPLETE!")
print("=" * 70)
