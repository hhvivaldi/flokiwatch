"""
TEST: Agent Data Package - Verify all 6 improvements
Builds a data package with real MT5 data and shows the output.
"""

import os
import sys
import json
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import MetaTrader5 as mt5
import pandas as pd
import config
from db_writer import get_recent_agent_decisions, get_trade_feedback, init_db
from agent_data_builder import build_data_package
from executor import get_account_balance


def main():
    print("=" * 70)
    print("AGENT DATA PACKAGE TEST - Verifying 6 Improvements")
    print("=" * 70)
    print(f"Timestamp: {datetime.now().isoformat()}")
    print()
    
    # Initialize MT5
    if not mt5.initialize():
        print("ERROR: Failed to initialize MT5")
        return
    
    print("✅ MT5 initialized")
    
    # Initialize DB
    init_db()
    print("✅ SQLite DB initialized")
    
    # ================================================================
    # Fetch real data from MT5
    # ================================================================
    
    # H1 candles
    h1_rates = mt5.copy_rates_from_pos(config.SYMBOL, mt5.TIMEFRAME_H1, 0, 20)
    h1_candles = []
    if h1_rates is not None:
        for r in h1_rates:
            h1_candles.append({
                "time": datetime.fromtimestamp(r["time"]).isoformat(),
                "open": float(r["open"]),
                "high": float(r["high"]),
                "low": float(r["low"]),
                "close": float(r["close"]),
                "tick_volume": int(r["tick_volume"]),
            })
    print(f"✅ H1 candles: {len(h1_candles)}")
    
    # M5 candles
    m5_rates = mt5.copy_rates_from_pos(config.SYMBOL, mt5.TIMEFRAME_M5, 0, 10)
    m5_candles = []
    if m5_rates is not None:
        for r in m5_rates:
            m5_candles.append({
                "time": datetime.fromtimestamp(r["time"]).isoformat(),
                "open": float(r["open"]),
                "high": float(r["high"]),
                "low": float(r["low"]),
                "close": float(r["close"]),
                "tick_volume": int(r["tick_volume"]),
            })
    print(f"✅ M5 candles: {len(m5_candles)}")
    
    # D1 candles (NEW)
    d1_rates = mt5.copy_rates_from_pos(config.SYMBOL, mt5.TIMEFRAME_D1, 0, 10)
    d1_candles = []
    if d1_rates is not None:
        for r in d1_rates:
            d1_candles.append({
                "time": datetime.fromtimestamp(r["time"]).isoformat(),
                "open": float(r["open"]),
                "high": float(r["high"]),
                "low": float(r["low"]),
                "close": float(r["close"]),
                "tick_volume": int(r["tick_volume"]),
            })
    print(f"✅ D1 candles: {len(d1_candles)}")
    
    # H4 candles (NEW)
    h4_rates = mt5.copy_rates_from_pos(config.SYMBOL, mt5.TIMEFRAME_H4, 0, 15)
    h4_candles = []
    if h4_rates is not None:
        for r in h4_rates:
            h4_candles.append({
                "time": datetime.fromtimestamp(r["time"]).isoformat(),
                "open": float(r["open"]),
                "high": float(r["high"]),
                "low": float(r["low"]),
                "close": float(r["close"]),
                "tick_volume": int(r["tick_volume"]),
            })
    print(f"✅ H4 candles: {len(h4_candles)}")
    
    # Current price
    tick = mt5.symbol_info_tick(config.SYMBOL)
    current_price = {"bid": tick.bid, "ask": tick.ask, "spread": (tick.ask - tick.bid) / 0.1}
    print(f"✅ Current price: {tick.bid}")
    
    # ================================================================
    # NEW DATA: Agent Memory
    # ================================================================
    agent_memory = get_recent_agent_decisions(5)
    print(f"✅ Agent memory: {len(agent_memory)} decisions")
    
    # ================================================================
    # NEW DATA: Trade Feedback
    # ================================================================
    trade_feedback = get_trade_feedback(5)
    print(f"✅ Trade feedback: {len(trade_feedback.get('last_trades', []))} trades")
    
    # ================================================================
    # NEW DATA: Delta Context (simulated - first call will be null)
    # ================================================================
    delta_context = {
        "price_change_pips": 12.5,
        "rsi_change": -3.2,
        "volume_change_pct": 25.0,
        "significant_events": ["Price moved 12 pips up"],
    }
    print("✅ Delta context: simulated")
    
    # ================================================================
    # NEW DATA: Portfolio
    # ================================================================
    account_balance = get_account_balance() or config.CAPITAL_INICIAL
    portfolio_data = {
        "daily_pnl": -23.27,  # From the log output
        "daily_wins": 1,
        "daily_losses": 1,
        "win_rate_today": 50.0,
        "drawdown_pct": 0.23,
        "risk_budget_remaining_pct": 88.4,
    }
    print(f"✅ Portfolio: balance=${account_balance:.2f}")
    
    # ================================================================
    # NEW DATA: Regime Context
    # ================================================================
    regime_context = {
        "regime": "ranging",
        "adx_hours_above_25": 3,
        "atr_vs_weekly_avg": 0.95,
        "trend_strength": "weak",
    }
    print("✅ Regime context: simulated")
    
    # ================================================================
    # Mock Brain result and other data
    # ================================================================
    class MockBrainResult:
        decision = "HOLD"
        final_score = 39.3
        confidence = 69.0
        confidence_level = "HIGH"
        scenario = "near_sr_zone"
        scenario_description = "Near strong FLIP zone at 5108.61"
        adjusted_scores = {"technical": 32.0, "ml": 37.9, "momentum": 55.0, "news": 58.4, "calendar": 50.0}
        adjusted_weights = {"technical": 0.30, "ml": 0.25, "momentum": 0.15, "news": 0.20, "calendar": 0.10}
        confirmations = ["Near strong S/R zone"]
        alerts = ["ML bearish"]
        mtf_trend = {"d1_direction": "bearish", "h4_direction": "neutral", "alignment": "mixed"}
        volume_gate = {"volume_ratio": 1.0, "status": "normal"}
    
    tech_data = {
        "rsi": {"value": 45.2, "level": "neutral"},
        "macd": {"histogram": -0.5, "signal": "bearish"},
        "ema": {"ema9": 2912, "ema21": 2908, "ema50": 2900},
        "bollinger": {"upper": 2930, "middle": 2915, "lower": 2900, "position": 0.5},
    }
    
    momentum_data = {
        "adx": {"adx_value": 22, "plus_di": 18, "minus_di": 24, "adx_classification": "weak"},
        "atr": {"atr_value": 28.5, "atr_trend": "stable"},
        "volume": {"volume_ratio": 1.0, "volume_classification": "normal"},
    }
    
    ml_data = {
        "prediction": "bearish",
        "max_confidence": 0.66,
        "h1_bullish_prob": 0.30,
        "h4_bullish_prob": 0.42,
    }
    
    news_data = {
        "headlines": ["Gold steady amid mixed signals"],
        "dxy": {"value": 99.04, "change_24h": 0.1, "trend": "stable"},
        "vix": {"value": 23.75, "level": "elevated"},
        "yields": {"value": 4.15, "trend": "rising"},
        "sentiment": {"normalized": 0.1, "label": "neutral"},
    }
    
    calendar_data = {
        "phase": "normal",
        "bias": "NEUTRAL",
        "score": 50,
    }
    
    session_context = {
        "session_name": "London",
        "hour_utc": 8,
        "today_trades": 2,
        "today_wins": 1,
        "today_losses": 1,
        "today_pnl": -23.27,
    }
    
    volatility_status = {"status": "NORMAL"}
    
    # ================================================================
    # Build the data package
    # ================================================================
    print()
    print("Building data package...")
    
    data_package = build_data_package(
        brain_result=MockBrainResult(),
        tech_data=tech_data,
        ml_data=ml_data,
        momentum_data=momentum_data,
        news_data=news_data,
        calendar_data=calendar_data,
        h1_candles=h1_candles,
        m5_candles=m5_candles,
        current_price=current_price,
        positions=[],
        session_context=session_context,
        volatility_status=volatility_status,
        sr_zones=[],
        candlestick_patterns=None,
        sr_proximity=None,
        d1_candles=d1_candles,
        h4_candles=h4_candles,
        agent_memory=agent_memory,
        trade_feedback=trade_feedback,
        delta_context=delta_context,
        portfolio=portfolio_data,
        regime_context=regime_context,
    )
    
    # ================================================================
    # Output results
    # ================================================================
    output_lines = []
    output_lines.append("=" * 70)
    output_lines.append("AGENT DATA PACKAGE TEST RESULTS")
    output_lines.append(f"Generated: {datetime.now().isoformat()}")
    output_lines.append("=" * 70)
    output_lines.append("")
    
    # Summary of 6 improvements
    output_lines.append("=" * 70)
    output_lines.append("SUMMARY: 6 NEW IMPROVEMENTS")
    output_lines.append("=" * 70)
    
    # 1. Agent Memory
    output_lines.append("")
    output_lines.append("1. AGENT MEMORY (agent_memory)")
    output_lines.append("-" * 40)
    am = data_package.get("agent_memory", {})
    decisions = am.get("recent_decisions", [])
    output_lines.append(f"   Entries: {len(decisions)}")
    if decisions:
        for i, d in enumerate(decisions[:3]):
            output_lines.append(f"   [{i+1}] {d.get('time')} | {d.get('trigger')} | {d.get('decision')}")
            output_lines.append(f"       Reasoning: {d.get('reasoning_summary', '')[:80]}...")
    else:
        output_lines.append("   (empty - no previous Agent decisions in DB)")
    
    # 2. D1 Candles
    output_lines.append("")
    output_lines.append("2. D1 CANDLES (d1_candles)")
    output_lines.append("-" * 40)
    d1 = data_package.get("d1_candles", [])
    output_lines.append(f"   Count: {len(d1)}")
    if d1:
        output_lines.append(f"   First: {d1[0].get('time')} O:{d1[0].get('o')} H:{d1[0].get('h')} L:{d1[0].get('l')} C:{d1[0].get('c')}")
        output_lines.append(f"   Last:  {d1[-1].get('time')} O:{d1[-1].get('o')} H:{d1[-1].get('h')} L:{d1[-1].get('l')} C:{d1[-1].get('c')}")
    
    # 3. H4 Candles
    output_lines.append("")
    output_lines.append("3. H4 CANDLES (h4_candles)")
    output_lines.append("-" * 40)
    h4 = data_package.get("h4_candles", [])
    output_lines.append(f"   Count: {len(h4)}")
    if h4:
        output_lines.append(f"   First: {h4[0].get('time')} O:{h4[0].get('o')} H:{h4[0].get('h')} L:{h4[0].get('l')} C:{h4[0].get('c')}")
        output_lines.append(f"   Last:  {h4[-1].get('time')} O:{h4[-1].get('o')} H:{h4[-1].get('h')} L:{h4[-1].get('l')} C:{h4[-1].get('c')}")
    
    # 4. Trade Feedback
    output_lines.append("")
    output_lines.append("4. TRADE FEEDBACK (trade_feedback)")
    output_lines.append("-" * 40)
    tf = data_package.get("trade_feedback", {})
    trades = tf.get("last_trades", [])
    accuracy = tf.get("agent_accuracy", {})
    output_lines.append(f"   Trades: {len(trades)}")
    output_lines.append(f"   Accuracy: total={accuracy.get('total_decisions', 0)}, correct_rejects={accuracy.get('correct_rejects', 0)}, incorrect_rejects={accuracy.get('incorrect_rejects', 0)}")
    if trades:
        for t in trades[:3]:
            output_lines.append(f"   #{t.get('ticket')} {t.get('direction')} P&L:{t.get('pnl')} | Agent:{t.get('agent_decision')} Right:{t.get('agent_was_right')}")
    
    # 5. Delta Context
    output_lines.append("")
    output_lines.append("5. DELTA CONTEXT (delta_context)")
    output_lines.append("-" * 40)
    dc = data_package.get("delta_context", {})
    output_lines.append(f"   price_change_pips: {dc.get('price_change_pips')}")
    output_lines.append(f"   rsi_change: {dc.get('rsi_change')}")
    output_lines.append(f"   volume_change_pct: {dc.get('volume_change_pct')}")
    output_lines.append(f"   significant_events: {dc.get('significant_events')}")
    
    # 6. Portfolio
    output_lines.append("")
    output_lines.append("6. PORTFOLIO (portfolio)")
    output_lines.append("-" * 40)
    pf = data_package.get("portfolio", {})
    output_lines.append(f"   daily_pnl: ${pf.get('daily_pnl')}")
    output_lines.append(f"   daily_wins: {pf.get('daily_wins')}")
    output_lines.append(f"   daily_losses: {pf.get('daily_losses')}")
    output_lines.append(f"   win_rate_today: {pf.get('win_rate_today')}%")
    output_lines.append(f"   drawdown_pct: {pf.get('drawdown_pct')}%")
    output_lines.append(f"   risk_budget_remaining_pct: {pf.get('risk_budget_remaining_pct')}%")
    
    # 7. Regime Context
    output_lines.append("")
    output_lines.append("7. REGIME CONTEXT (regime_context)")
    output_lines.append("-" * 40)
    rc = data_package.get("regime_context", {})
    output_lines.append(f"   regime: {rc.get('regime')}")
    output_lines.append(f"   adx_hours_above_25: {rc.get('adx_hours_above_25')}")
    output_lines.append(f"   atr_vs_weekly_avg: {rc.get('atr_vs_weekly_avg')}")
    output_lines.append(f"   trend_strength: {rc.get('trend_strength')}")
    
    output_lines.append("")
    output_lines.append("=" * 70)
    output_lines.append("FULL DATA PACKAGE (JSON)")
    output_lines.append("=" * 70)
    output_lines.append("")
    output_lines.append(json.dumps(data_package, indent=2, default=str))
    
    # Print to console
    for line in output_lines:
        print(line)
    
    # Save to file
    output_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "agent_data_package_test.txt")
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write("\n".join(output_lines))
    
    print()
    print(f"✅ Output saved to: {output_path}")
    
    mt5.shutdown()
    print("✅ MT5 shutdown")


if __name__ == "__main__":
    main()
